"""OPC UA namespace definition: object types, object creation, snapshot/event writes."""

from __future__ import annotations

import asyncio
import inspect
import json
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from asyncua import Node, Server, ua
from asyncua.common.instantiate_util import instantiate
from asyncua.common.methods import uamethod

from terminal_model.port import EquipmentEvent, EquipmentSnapshot
from terminal_model.types import Kind

from terminal_opcua.codec import event_to_json

NAMESPACE_URI = "urn:terminal:model"

EQUIPMENT_VARS = (
    "Id", "Kind", "Vendor", "State", "PositionX", "PositionY",
    "CurrentJobId", "EnergyKwh", "Extra", "LastEvent", "EventSeq",
)

KIND_VARS: dict[Kind, tuple[str, ...]] = {
    Kind.STS: ("BerthSide", "SpreaderHeightM", "TrolleyPositionM", "HandoverLaneOccupied"),
    Kind.AGV: ("BatteryPct", "LoadedContainerId", "Route", "SpeedMps"),
    Kind.ASC: ("BlockId", "GantryPositionM", "WsTpOccupied", "LsTpOccupied"),
}

EXTRA_KEY_MAP: dict[str, str] = {
    "berth_side": "BerthSide",
    "spreader_height_m": "SpreaderHeightM",
    "trolley_position_m": "TrolleyPositionM",
    "handover_lane_occupied": "HandoverLaneOccupied",
    "battery_pct": "BatteryPct",
    "loaded_container_id": "LoadedContainerId",
    "route": "Route",
    "speed_mps": "SpeedMps",
    "block_id": "BlockId",
    "gantry_position_m": "GantryPositionM",
    "ws_tp_occupied": "WsTpOccupied",
    "ls_tp_occupied": "LsTpOccupied",
}

_S, _D, _B, _U64 = (
    ua.VariantType.String, ua.VariantType.Double,
    ua.VariantType.Boolean, ua.VariantType.UInt64,
)

VAR_TYPES: dict[str, ua.VariantType] = {
    "Id": _S, "Kind": _S, "Vendor": _S, "State": _S,
    "PositionX": _D, "PositionY": _D, "CurrentJobId": _S, "EnergyKwh": _D,
    "Extra": _S, "LastEvent": _S, "EventSeq": _U64,
    "BerthSide": _S, "SpreaderHeightM": _D, "TrolleyPositionM": _D,
    "HandoverLaneOccupied": _B,
    "BatteryPct": _D, "LoadedContainerId": _S, "Route": _S, "SpeedMps": _D,
    "BlockId": _S, "GantryPositionM": _D, "WsTpOccupied": _B, "LsTpOccupied": _B,
}

_DEFAULTS = {_S: "", _D: 0.0, _B: False, _U64: 0}


@dataclass
class Types:
    equipment: ua.NodeId
    sts: ua.NodeId
    agv: ua.NodeId
    asc: ua.NodeId
    control: ua.NodeId


@dataclass
class EquipmentHandlers:
    """Method handlers; each may be a plain function or a coroutine function."""

    assign_job: Callable[[str], bool | Awaitable[bool]]
    cancel_job: Callable[[], bool | Awaitable[bool]]
    reset: Callable[[], bool | Awaitable[bool]]


async def _resolve(result):
    if inspect.isawaitable(result):
        return await result
    return result


def to_variant(name: str, value: object) -> ua.Variant:
    vt = VAR_TYPES[name]
    if vt is _S:
        value = "" if value is None else str(value)
    elif vt is _D:
        value = float(value)  # type: ignore[arg-type]
    elif vt is _B:
        value = bool(value)
    return ua.Variant(value, vt)


async def _add_var(parent: Node, idx: int, name: str) -> None:
    vt = VAR_TYPES[name]
    v = await parent.add_variable(idx, name, ua.Variant(_DEFAULTS[vt], vt))
    await v.set_modelling_rule(True)


async def register_types(server: Server) -> tuple[int, Types]:
    idx = await server.register_namespace(NAMESPACE_URI)
    base = server.nodes.base_object_type
    equipment = await base.add_object_type(idx, "EquipmentType")
    for name in EQUIPMENT_VARS:
        await _add_var(equipment, idx, name)
    kind_nodes: dict[Kind, Node] = {}
    for kind, name in ((Kind.STS, "STSType"), (Kind.AGV, "AGVType"), (Kind.ASC, "ASCType")):
        t = await equipment.add_object_type(idx, name)
        for var in KIND_VARS[kind]:
            await _add_var(t, idx, var)
        kind_nodes[kind] = t
    # Method declarations on the types carry no modelling rule, so instantiate()
    # skips them; per-instance methods are added in create_*_object.
    
    @uamethod
    async def noop_b(parent, *a):
        return True

    @uamethod
    async def noop_stop(parent):
        return None

    in_s = ua.Argument()
    in_s.Name = "jobJson"
    in_s.DataType = ua.NodeId(ua.ObjectIds.String)
    in_s.ValueRank = -1
    await equipment.add_method(idx, "AssignJob", noop_b, [in_s], [_B])
    await equipment.add_method(idx, "CancelJob", noop_b, [], [_B])
    await equipment.add_method(idx, "Reset", noop_b, [], [_B])
    control = await base.add_object_type(idx, "ControlType")
    await control.add_method(idx, "Stop", noop_stop, [], [])
    in_j = ua.Argument()
    in_j.Name = "json"
    in_j.DataType = ua.NodeId(ua.ObjectIds.String)
    in_j.ValueRank = -1
    await control.add_method(idx, "PrefillYard", noop_b, [in_j], [_B])
    await control.add_method(idx, "Start", noop_b, [], [_B])
    return idx, Types(
        equipment=equipment.nodeid,
        sts=kind_nodes[Kind.STS].nodeid,
        agv=kind_nodes[Kind.AGV].nodeid,
        asc=kind_nodes[Kind.ASC].nodeid,
        control=control.nodeid,
    )


def _string_arg(name: str) -> ua.Argument:
    a = ua.Argument()
    a.Name = name
    a.DataType = ua.NodeId(ua.ObjectIds.String)
    a.ValueRank = -1
    return a


# Method wrappers are coroutines: asyncua awaits them on the event loop, so the
# synchronous handlers run on the loop thread (sync wrappers would run in a
# ThreadPoolExecutor and race with the sim/runner loop). A handler that returns
# an awaitable (coroutine function, e.g. the gateway forwarding upstream) is awaited.
async def create_equipment_object(
    server: Server, idx: int, types: Types, parent: Node,
    snap: EquipmentSnapshot, handlers: EquipmentHandlers,
) -> Node:
    type_id = {Kind.STS: types.sts, Kind.AGV: types.agv, Kind.ASC: types.asc}[snap.kind]
    nodes = await instantiate(
        parent, server.get_node(type_id), bname=f"{idx}:{snap.equipment_id}", idx=idx)
    obj = nodes[0]

    @uamethod
    async def assign(_parent, job_json):
        return bool(await _resolve(handlers.assign_job(job_json)))

    @uamethod
    async def cancel(_parent):
        return bool(await _resolve(handlers.cancel_job()))

    @uamethod
    async def reset(_parent):
        return bool(await _resolve(handlers.reset()))

    await obj.add_method(idx, "AssignJob", assign, [_string_arg("jobJson")], [_B])
    await obj.add_method(idx, "CancelJob", cancel, [], [_B])
    await obj.add_method(idx, "Reset", reset, [], [_B])
    await write_snapshot(obj, snap)
    return obj


async def create_control_object(
    server: Server, idx: int, types: Types, parent: Node, *,
    stop: Callable[[], None | Awaitable[None]],
    prefill: Callable[[str], bool | Awaitable[bool]],
    start: Callable[[], bool | Awaitable[bool]],
) -> Node:
    nodes = await instantiate(
        parent, server.get_node(types.control), bname=f"{idx}:Clock", idx=idx)
    obj = nodes[0]
    for var in ("SimTime", "SpeedFactor"):
        await obj.add_variable(idx, var, 0.0, datatype=ua.NodeId(ua.ObjectIds.Double))

    @uamethod
    async def _stop(_parent):
        await _resolve(stop())

    @uamethod
    async def _prefill(_parent, json_str):
        return bool(await _resolve(prefill(json_str)))

    @uamethod
    async def _start(_parent):
        return bool(await _resolve(start()))

    await obj.add_method(idx, "Stop", _stop, [], [])
    await obj.add_method(idx, "PrefillYard", _prefill, [_string_arg("json")], [_B])
    await obj.add_method(idx, "Start", _start, [], [_B])
    return obj


async def _child_or_create(parent: Node, idx: int, name: str) -> Node:
    for c in await parent.get_children():
        if (await c.read_browse_name()).Name == name:
            return c
    return await parent.add_folder(idx, name)


async def ensure_folders(server: Server, idx: int, root: Node) -> dict[str, Node]:
    terminal = await _child_or_create(root, idx, "Terminal")
    return {
        "Terminal": terminal,
        "Quay": await _child_or_create(terminal, idx, "Quay"),
        "Fleet": await _child_or_create(terminal, idx, "Fleet"),
        "Yard": await _child_or_create(terminal, idx, "Yard"),
    }


async def block_folder(server: Server, idx: int, yard: Node, block: str) -> Node:
    return await _child_or_create(yard, idx, f"Block-{block}")


# NodeIds repeat across servers (numeric ids per namespace), so per-node state
# is scoped by the node's session (one per server) as well as by NodeId.
_VAR_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_EMITTERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def get_var(node: Node, name: str) -> Node:
    """Child variable of an equipment object by browse name (cached per object)."""
    cache = _VAR_CACHE.setdefault(node.session, {}).setdefault(node.nodeid, {})
    found = cache.get(name)
    if found is not None:
        return found
    for c in await node.get_children():
        if (await c.read_browse_name()).Name == name:
            cache[name] = c
            return c
    raise KeyError(name)


async def _write(node: Node, name: str, value: object) -> None:
    await (await get_var(node, name)).write_value(to_variant(name, value))


async def write_snapshot(node: Node, snap: EquipmentSnapshot) -> None:
    await _write(node, "Id", snap.equipment_id)
    await _write(node, "Kind", snap.kind.value)
    await _write(node, "Vendor", snap.vendor)
    await _write(node, "State", snap.state.value)
    await _write(node, "PositionX", snap.position.x)
    await _write(node, "PositionY", snap.position.y)
    await _write(node, "CurrentJobId", snap.current_job_id)
    await _write(node, "EnergyKwh", snap.energy_kwh)
    await _write(node, "Extra", json.dumps(dict(snap.extra)))
    allowed = set(KIND_VARS[snap.kind])
    for key, value in snap.extra.items():
        var = EXTRA_KEY_MAP.get(key)
        if var is None or var not in allowed:
            continue
        if var == "Route":
            value = json.dumps(value)
        await _write(node, var, value)


class EventEmitter:
    """Emits events on one equipment node; owns the in-memory EventSeq counter."""

    def __init__(self, node: Node) -> None:
        self.node = node
        self.seq = 0
        self._lock = asyncio.Lock()

    async def emit(self, ev: EquipmentEvent) -> None:
        # Lock keeps LastEvent/EventSeq in order; the counter is never re-read
        # from the node, so concurrent emits cannot lose increments.
        async with self._lock:
            self.seq += 1
            await _write(self.node, "LastEvent", event_to_json(ev))
            seq = await get_var(self.node, "EventSeq")
            await seq.write_value(ua.Variant(self.seq, ua.VariantType.UInt64))


async def emit_event(node: Node, ev: EquipmentEvent) -> None:
    """Compatibility wrapper: one EventEmitter per node, kept in a registry."""
    scope = _EMITTERS.setdefault(node.session, {})
    em = scope.get(node.nodeid)
    if em is None:
        em = scope[node.nodeid] = EventEmitter(node)
    await em.emit(ev)
