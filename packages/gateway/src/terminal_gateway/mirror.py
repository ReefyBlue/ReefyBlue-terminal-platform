"""Mirrors of one upstream OPC UA server (an equipment or the clock/control server)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from asyncua import Client, Node, Server, ua
from terminal_model.port import EquipmentEvent, EquipmentSnapshot, EventKind, LinkReason
from terminal_model.types import Kind, Position, State
from terminal_opcua.events import (
    EventEmitter,
    EventTypes,
    event_from_ua,
    resolve_event_types,
    subscribe_terminal_events,
)
from terminal_opcua.nodeset import (
    EQUIPMENT_VARS,
    KIND_VARS,
    NAMESPACE_URI,
    EquipmentHandlers,
    Types,
    create_control_object,
    create_equipment_object,
    get_var,
    to_variant,
)

log = logging.getLogger(__name__)

SUBSCRIPTION_PERIOD_MS = 50
METHOD_TIMEOUT_S = 2.0
RECONNECT_S = 5.0
CONNECT_TIMEOUT_S = 2.0
WATCHDOG_S = 1.0


def _job_id(job_json: str) -> str | None:
    """``job_id`` of an ``AssignJob`` argument; ``None`` if it is not a JSON object with one."""
    try:
        job_id = json.loads(job_json)["job_id"]
    except (TypeError, ValueError, KeyError):
        return None
    return job_id if isinstance(job_id, str) else None


async def _safe_disconnect(client: Client) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), 2.0)
    except Exception:  # dead transport: nothing to close cleanly
        log.debug("disconnect of %s failed", client.server_url, exc_info=True)


class _UpstreamLink:
    """Owns one asyncua client: connect, run until lost, reconnect every ``reconnect_s``."""

    name = "upstream"

    def __init__(self, endpoint: str, *, reconnect_s: float = RECONNECT_S) -> None:
        self.endpoint = endpoint
        self.reconnect_s = reconnect_s
        self._client: Client | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def connected(self) -> bool:
        return self._client is not None

    def _launch(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"mirror:{self.name}")

    async def _run(self) -> None:
        failures = 0
        while not self._stopping:
            client = Client(self.endpoint, timeout=CONNECT_TIMEOUT_S, watchdog_intervall=WATCHDOG_S)
            lost = asyncio.Event()

            async def on_lost(_exc: Exception, lost=lost) -> None:
                lost.set()

            client.connection_lost_callback = on_lost
            was_up = opened = False
            try:
                await client.connect()  # cleans up its own socket on failure
                opened = True
                await self._on_connected(client)
                self._client = client
                was_up = True
                failures = 0
                log.info("%s: connected to %s", self.name, self.endpoint)
                await lost.wait()
                log.warning("%s: connection to %s lost", self.name, self.endpoint)
            except asyncio.CancelledError:
                self._client = None
                if opened:
                    await _safe_disconnect(client)
                raise
            except Exception as e:  # refused, timeout, browse failure: retry later
                failures += 1
                level = logging.WARNING if failures == 1 or was_up else logging.DEBUG
                log.log(level, "%s: upstream %s unavailable: %r", self.name, self.endpoint, e)
            self._client = None
            if opened:
                await _safe_disconnect(client)
            if not self._stopping:
                await self._on_lost()
                await asyncio.sleep(self.reconnect_s)

    async def _on_connected(self, client: Client) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def _on_lost(self) -> None:
        return None

    async def _call(self, obj: Node | None, idx: int, method: str, *args):
        """Forward a method call upstream; ``None`` on any failure (logged)."""
        if obj is None or self._client is None:
            log.warning("%s: %s not forwarded, upstream unavailable", self.name, method)
            return None
        try:
            return await asyncio.wait_for(obj.call_method(f"{idx}:{method}", *args), METHOD_TIMEOUT_S)
        except Exception as e:
            log.warning("%s: forwarding %s failed: %r", self.name, method, e)
            return None

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


class EquipmentMirror(_UpstreamLink):
    """Gateway-side copy of one equipment object, kept in sync with its upstream server.

    Upstream OPC UA events for this equipment are re-emitted by the mirror's own
    ``EventEmitter``: the gateway Seq is the mirror's, strictly increasing and never
    derived from the upstream Seq. Events the upstream emitted while the link was
    down are lost; that shows up as an upstream Seq gap in the log (the device is
    OFFLINE during the outage and the ECS replans from the snapshot afterwards).

    A lost link is not a fault of the equipment: it is reported as ``State = OFFLINE``
    and STATE_CHANGED -> OFFLINE (``LinkReason.UPSTREAM_DISCONNECTED``), once per
    outage. On reconnect a STATE_CHANGED to the upstream's fresh state
    (``LinkReason.UPSTREAM_RECONNECTED``) ALWAYS follows, also when that state is FAULT,
    so the ECS knows the device is reachable again.

    Subscribe-before-read invariant: on (re)connect the data-change and event
    subscriptions are in place *before* the snapshot is read. OPC UA events have no
    initial value, so anything the upstream emits after subscribing is delivered, and
    anything before is covered by the snapshot read that follows. ``_write_lock`` is
    held from the snapshot read through the recovery event, and forwarded events take
    the same lock, so events arriving meanwhile queue behind the recovery event (FIFO)
    and the last STATE_CHANGED the ECS sees matches the upstream's latest state.
    Data changes take the lock too, so a newer value is never overwritten by the
    snapshot.
    Duplicates this can cause are harmless (a repeated STATE_CHANGED is idempotent for
    the ECS; JOB_COMPLETED for an unknown job is ignored).
    """

    def __init__(
        self, server: Server, idx: int, types: Types, parent: Node, *,
        equipment_id: str, kind: Kind, endpoint: str, reconnect_s: float = RECONNECT_S,
    ) -> None:
        super().__init__(endpoint, reconnect_s=reconnect_s)
        self.server = server
        self.idx = idx
        self.types = types
        self.parent = parent
        self.equipment_id = equipment_id
        self.name = equipment_id
        self.kind = kind
        self.var_names = (*EQUIPMENT_VARS, *KIND_VARS[kind])
        self.obj: Node | None = None
        self._sim_time = 0.0
        self._emitter: EventEmitter | None = None
        self._up_types: EventTypes | None = None
        # Last upstream Seq seen (0: none yet). Deliberately kept across reconnects, so
        # events lost during an outage show up as a Seq gap (spec section 5); a restarted
        # upstream (Seq back to 1) stays silent, as the gap rule only fires for
        # seq > _up_seq + 1.
        self._up_seq = 0
        self._fault_announced = False
        self._write_lock = asyncio.Lock()
        self._path: list[str] = []
        self._uobj: Node | None = None
        self._uidx = 0
        self._names: dict[ua.NodeId, str] = {}

    def set_sim_time(self, t: float) -> None:
        self._sim_time = float(t)

    async def start(self) -> None:
        snap = EquipmentSnapshot(
            equipment_id=self.equipment_id, kind=self.kind, vendor="", state=State.OFFLINE,
            position=Position(0.0, 0.0), current_job_id=None, energy_kwh=0.0, extra={},
        )
        handlers = EquipmentHandlers(
            assign_job=self._assign, cancel_job=self._cancel, reset=self._reset)
        self.obj = await create_equipment_object(
            self.server, self.idx, self.types, self.parent, snap, handlers)
        self._emitter = await EventEmitter.create(
            self.server, self.types.events, self.obj, self.equipment_id)
        self._path = await self._upstream_path()
        self._launch()

    async def _upstream_path(self) -> list[str]:
        """Browse names from ``Terminal`` down to the parent folder (same on upstream)."""
        names: list[str] = []
        node = self.parent
        while True:
            name = (await node.read_browse_name()).Name
            names.append(name)
            if name == "Terminal":
                break
            node = await node.get_parent()
            if node is None:
                raise ValueError(f"{self.equipment_id}: parent is not under Terminal")
        return [*reversed(names), self.equipment_id]

    # -- methods forwarded upstream ------------------------------------------------
    async def _forward_bool(self, method: str, *args) -> bool:
        return bool(await self._call(self._uobj, self._uidx, method, *args))

    async def _assign(self, job_json: str) -> bool:
        """Forward ``AssignJob``, except for a job the upstream already runs or ran.

        The ECS re-issues a job id when a forwarded call timed out (e.g. during a link
        cut) although the upstream executed it; running it again would repeat the job.
        A job id equal to the mirrored ``CurrentJobId`` or ``LastJobId`` is answered
        True without forwarding. Unparseable JSON is forwarded (the upstream rejects it).
        """
        job_id = _job_id(job_json)
        if job_id:
            for name in ("CurrentJobId", "LastJobId"):
                if await self._mirrored(name) == job_id:
                    log.info("assign %s: already %s upstream, idempotent True", job_id, name)
                    return True
        return await self._forward_bool("AssignJob", job_json)

    async def _mirrored(self, name: str) -> object:
        """The mirrored value of ``name`` (``None`` if it cannot be read)."""
        if self.obj is None:
            return None
        try:
            return await (await get_var(self.obj, name)).read_value()
        except Exception:
            log.exception("%s: reading mirrored %s failed", self.equipment_id, name)
            return None

    async def _cancel(self) -> bool:
        return await self._forward_bool("CancelJob")

    async def _reset(self) -> bool:
        return await self._forward_bool("Reset")

    # -- writes -------------------------------------------------------------------
    async def _write(self, name: str, value: object) -> None:
        assert self.obj is not None
        await (await get_var(self.obj, name)).write_value(to_variant(name, value))

    async def _emit(self, ev: EquipmentEvent) -> None:
        assert self._emitter is not None
        await self._emitter.emit(ev)

    async def _on_connected(self, client: Client) -> None:
        uidx = await client.get_namespace_index(NAMESPACE_URI)
        uobj = await client.nodes.objects.get_child([f"{uidx}:{n}" for n in self._path])
        up_types = await resolve_event_types(client)
        nodes = [await uobj.get_child(f"{uidx}:{n}") for n in self.var_names]
        self._names = {n.nodeid: name for n, name in zip(nodes, self.var_names, strict=True)}
        self._up_types = up_types
        # Subscribe before reading the snapshot (see class docstring): an event emitted
        # upstream in between would otherwise be lost for good.
        sub = await client.create_subscription(SUBSCRIPTION_PERIOD_MS, self)
        await sub.subscribe_data_change(nodes, sampling_interval=0.0)
        await subscribe_terminal_events(client, sub, up_types)
        async with self._write_lock:  # forwarded events queue behind the recovery
            values = await client.read_values(nodes)
            current = dict(zip(self.var_names, values, strict=True))
            for name, value in current.items():
                await self._write(name, value)
            if self._fault_announced:
                await self._announce_recovery(current["State"])
            self._fault_announced = False
        self._uobj, self._uidx = uobj, uidx

    async def _announce_recovery(self, upstream_state: object) -> None:
        """After an OFFLINE report: emit the upstream's fresh state (caller holds the write
        lock), always, also when it is FAULT: the ECS treats OFFLINE as unknown until it
        sees this event. An unknown state string is logged and yields no event."""
        try:
            state = State(upstream_state)
        except ValueError:
            log.warning("%s: upstream State %r unknown; no recovery event",
                        self.equipment_id, upstream_state)
            return
        await self._emit(EquipmentEvent(
            EventKind.STATE_CHANGED, self.equipment_id, self._sim_time,
            reason=LinkReason.UPSTREAM_RECONNECTED.value, new_state=state))

    async def event_notification(self, event) -> None:
        if self._up_types is None or self.obj is None:
            return
        try:
            seq, ev = event_from_ua(event, self._up_types)
        except ValueError as e:
            log.warning("%s: undecodable upstream event skipped: %s", self.equipment_id, e)
            return
        if ev.equipment_id != self.equipment_id:
            return
        # Each notification runs in its own asyncua task; no await before the lock, so
        # tasks reach it in delivery order and asyncio.Lock (FIFO) keeps that order.
        async with self._write_lock:
            if self._up_seq and seq > self._up_seq + 1:
                log.warning("%s: upstream Seq gap %d -> %d", self.equipment_id, self._up_seq, seq)
            self._up_seq = seq
            try:
                await self._emit(ev)
            except Exception:
                log.exception("%s: re-emitting upstream event failed", self.equipment_id)

    async def datachange_notification(self, node: Node, val, data) -> None:
        name = self._names.get(node.nodeid)
        if name is None or self.obj is None:
            return
        # Same lock as the snapshot write: a newer value that arrives during the
        # snapshot read is written after it, not overwritten by it.
        async with self._write_lock:
            try:
                await self._write(name, val)
            except Exception:
                log.exception("%s: mirroring %s failed", self.equipment_id, name)

    async def status_change_notification(self, status) -> None:
        """Subscription status changes are handled by the connection-loss path."""

    async def _on_lost(self) -> None:
        self._uobj = None
        if self._fault_announced or self.obj is None:
            return
        self._fault_announced = True
        ev = EquipmentEvent(
            EventKind.STATE_CHANGED, self.equipment_id, self._sim_time,
            reason=LinkReason.UPSTREAM_DISCONNECTED.value, new_state=State.OFFLINE)
        try:
            async with self._write_lock:
                await self._write("State", State.OFFLINE.value)
                await self._emit(ev)
        except Exception:
            log.exception("%s: writing OFFLINE failed", self.equipment_id)


class ControlMirror(_UpstreamLink):
    """``Terminal/Clock`` on the gateway: mirrors SimTime/SpeedFactor, forwards Stop/PrefillYard/Start."""

    name = "control"

    def __init__(
        self, server: Server, idx: int, types: Types, parent: Node, *, endpoint: str,
        on_sim_time: Callable[[float], None], reconnect_s: float = RECONNECT_S,
    ) -> None:
        super().__init__(endpoint, reconnect_s=reconnect_s)
        self.server = server
        self.idx = idx
        self.types = types
        self.parent = parent
        self.on_sim_time = on_sim_time
        self.obj: Node | None = None
        self._uobj: Node | None = None
        self._uidx = 0
        self._names: dict[ua.NodeId, str] = {}

    async def start(self) -> None:
        self.obj = await create_control_object(
            self.server, self.idx, self.types, self.parent, stop=self._stop, prefill=self._prefill,
            start=self._start)
        self._launch()

    async def _stop(self) -> None:
        await self._call(self._uobj, self._uidx, "Stop")

    async def _prefill(self, json_str: str) -> bool:
        return bool(await self._call(self._uobj, self._uidx, "PrefillYard", json_str))

    async def _start(self) -> bool:
        return bool(await self._call(self._uobj, self._uidx, "Start"))

    async def _write(self, name: str, value: float) -> None:
        assert self.obj is not None
        await (await get_var(self.obj, name)).write_value(
            ua.Variant(float(value), ua.VariantType.Double))
        if name == "SimTime":
            self.on_sim_time(float(value))

    async def _on_connected(self, client: Client) -> None:
        uidx = await client.get_namespace_index(NAMESPACE_URI)
        uobj = await client.nodes.objects.get_child([f"{uidx}:Terminal", f"{uidx}:Clock"])
        names = ("SimTime", "SpeedFactor")
        nodes = [await uobj.get_child(f"{uidx}:{n}") for n in names]
        for name, value in zip(names, await client.read_values(nodes), strict=True):
            await self._write(name, value)
        self._names = {n.nodeid: name for n, name in zip(nodes, names, strict=True)}
        self._uobj, self._uidx = uobj, uidx
        sub = await client.create_subscription(SUBSCRIPTION_PERIOD_MS, self)
        await sub.subscribe_data_change(nodes, sampling_interval=0.0)

    async def datachange_notification(self, node: Node, val, data) -> None:
        name = self._names.get(node.nodeid)
        if name is None or self.obj is None:
            return
        try:
            await self._write(name, val)
        except Exception:
            log.exception("control: mirroring %s failed", name)

    async def status_change_notification(self, status) -> None:
        """Subscription status changes are handled by the connection-loss path."""

    async def _on_lost(self) -> None:
        self._uobj = None
