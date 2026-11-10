"""Equipment events as real OPC UA Events: type hierarchy, server emitter, client decoder."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from asyncua import Client, Node, Server, ua

from terminal_model.port import EquipmentEvent, EventKind
from terminal_model.types import State

from terminal_opcua.codec import event_fields, event_from_fields

NAMESPACE_URI = "urn:terminal:model"  # re-exported by nodeset, which imports this module

BASE_EVENT_TYPE_NAME = "TerminalEventType"

EVENT_FIELDS: tuple[tuple[str, ua.VariantType], ...] = (
    ("EquipmentId", ua.VariantType.String),
    ("Seq", ua.VariantType.UInt64),
    ("SimTime", ua.VariantType.Double),
    ("JobId", ua.VariantType.String),
    ("Reason", ua.VariantType.String),
    ("NewState", ua.VariantType.String),
)

EVENT_TYPE_NAMES: dict[EventKind, str] = {
    EventKind.STATE_CHANGED: "StateChangedEventType",
    EventKind.JOB_COMPLETED: "JobCompletedEventType",
    EventKind.JOB_FAILED: "JobFailedEventType",
}

EVENT_QUEUE = 100

SEVERITY_NORMAL = 100
SEVERITY_FAULT = 500


@dataclass(frozen=True)
class EventTypes:
    base: ua.NodeId
    state_changed: ua.NodeId
    job_completed: ua.NodeId
    job_failed: ua.NodeId

    def node_for(self, kind: EventKind) -> ua.NodeId:
        return {
            EventKind.STATE_CHANGED: self.state_changed,
            EventKind.JOB_COMPLETED: self.job_completed,
            EventKind.JOB_FAILED: self.job_failed,
        }[kind]

    def kind_of(self, event_type: ua.NodeId) -> EventKind:
        for kind in EventKind:
            if self.node_for(kind) == event_type:
                return kind
        raise ValueError(f"unknown event type: {event_type}")


async def register_event_types(server: Server, idx: int) -> EventTypes:
    base = await server.create_custom_event_type(
        idx, BASE_EVENT_TYPE_NAME, ua.ObjectIds.BaseEventType, list(EVENT_FIELDS))
    subs = {
        kind: await server.create_custom_event_type(idx, name, base.nodeid, [])
        for kind, name in EVENT_TYPE_NAMES.items()
    }
    return EventTypes(
        base=base.nodeid,
        state_changed=subs[EventKind.STATE_CHANGED].nodeid,
        job_completed=subs[EventKind.JOB_COMPLETED].nodeid,
        job_failed=subs[EventKind.JOB_FAILED].nodeid,
    )


async def resolve_event_types(client: Client) -> EventTypes:
    idx = await client.get_namespace_index(NAMESPACE_URI)
    base = await client.nodes.base_event_type.get_child(f"{idx}:{BASE_EVENT_TYPE_NAME}")
    subs = {
        kind: (await base.get_child(f"{idx}:{name}")).nodeid
        for kind, name in EVENT_TYPE_NAMES.items()
    }
    return EventTypes(
        base=base.nodeid,
        state_changed=subs[EventKind.STATE_CHANGED],
        job_completed=subs[EventKind.JOB_COMPLETED],
        job_failed=subs[EventKind.JOB_FAILED],
    )


def message_for(ev: EquipmentEvent) -> str:
    msg = f"{ev.kind.value} {ev.equipment_id}"
    if ev.job_id:
        msg += f" job={ev.job_id}"
    if ev.reason:
        msg += f" reason={ev.reason}"
    if ev.new_state:
        msg += f" state={ev.new_state.value}"
    return msg


def _severity(ev: EquipmentEvent) -> int:
    if ev.kind is EventKind.JOB_FAILED or ev.new_state is State.FAULT:
        return SEVERITY_FAULT
    return SEVERITY_NORMAL


class EventEmitter:
    """Emits OPC UA events for one equipment object; owns its in-memory Seq counter.

    Events are emitted via the Server node (asyncua delivers only to subscribers of
    the emitting node), with SourceNode/SourceName set to the equipment object.
    """

    def __init__(self, generators: dict[EventKind, Any]) -> None:
        self._generators = generators
        self.seq = 0
        self._lock = asyncio.Lock()

    @classmethod
    async def create(
        cls, server: Server, types: EventTypes, node: Node, equipment_id: str,
    ) -> EventEmitter:
        generators: dict[EventKind, Any] = {}
        for kind in EventKind:
            gen = await server.get_event_generator(types.node_for(kind))
            # init() only fills SourceNode/SourceName when empty, so set them after.
            gen.event.SourceNode = node.nodeid
            gen.event.SourceName = equipment_id
            generators[kind] = gen
        return cls(generators)

    async def emit(self, ev: EquipmentEvent) -> None:
        # The lock keeps Seq strictly increasing in trigger order under concurrent emits.
        async with self._lock:
            self.seq += 1
            gen = self._generators[ev.kind]
            for name, value in event_fields(ev).items():
                setattr(gen.event, name, value)
            gen.event.Seq = self.seq
            gen.event.Severity = _severity(ev)
            await gen.trigger(message=message_for(ev))


def event_from_ua(event: Any, types: EventTypes) -> tuple[int, EquipmentEvent]:
    """Decode a client-side asyncua Event into (seq, EquipmentEvent); ValueError if not ours."""
    try:
        kind = types.kind_of(event.EventType)
        fields = {name: getattr(event, name) for name, _ in EVENT_FIELDS}
        return int(fields["Seq"]), event_from_fields(kind, fields)
    except ValueError:
        raise
    except (TypeError, AttributeError, KeyError) as e:
        raise ValueError(f"undecodable event: {e!r}") from e


async def subscribe_terminal_events(client: Client, sub: Any, types: EventTypes) -> int:
    return await sub.subscribe_events(
        client.nodes.server, evtypes=[types.base], queuesize=EVENT_QUEUE)
