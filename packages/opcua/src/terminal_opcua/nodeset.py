"""OPC UA namespace definition: object types, object creation, snapshot writes."""

from __future__ import annotations

import datetime
import inspect
import json
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from asyncua import Node, Server, ua
from asyncua.common.instantiate_util import instantiate
from asyncua.common.methods import uamethod
from terminal_model.port import EquipmentSnapshot
from terminal_model.types import Kind

# NAMESPACE_URI lives in events (which nodeset imports) and stays importable from here.
from terminal_opcua.events import NAMESPACE_URI, EventTypes, register_event_types

EQUIPMENT_VARS = (
    "Id", "Kind", "Vendor", "State", "PositionX", "PositionY",
    "CurrentJobId", "EnergyKwh", "Extra",
    # outcome of the most recent job that ended ("" before the first): lets a controller
    # reconcile a job whose JOB_COMPLETED/JOB_FAILED it missed during a link outage
    "LastJobId", "LastJobOutcome",
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
    "last_job_id": "LastJobId",
    "last_job_outcome": "LastJobOutcome",
}

# v2 equipment (C1b, layouts with a metre ``terminal``): extra variables on subtypes of the kind
# types (``STS2Type`` < ``STSType`` ...), registered only by ``register_types(v2=True)``, so a
# legacy (v1) address space is unchanged. ``Extra`` keeps carrying the whole extra dict; these
# are typed views of it (Konecranes ECS screens, reference doc section 6) plus three derived
# ones, ``V2_COMMON_VARS``.
V2_COMMON_VARS = ("TechnicalStatus", "OperationalMode", "LastDataReceivedAt")

V2_KIND_VARS: dict[Kind, tuple[str, ...]] = {
    Kind.STS: (*V2_COMMON_VARS, "GantryPos_m", "TrolleyPos_m", "HoistPos_m", "LaneId", "Holding",
               "LaneBuffer", "LaneBufferCount", "WaitForAgv_s", "WaitForAgvTotal_s"),
    Kind.AGV: (*V2_COMMON_VARS, "Heading_deg", "Speed_mps", "LoadA", "LoadB", "ChargePct",
               "AtPoint"),
    Kind.ASC: (*V2_COMMON_VARS, "Bay", "Row", "Tier", "TrolleyPos_m", "HoistPos_m", "Partner",
               "Role", "Holding"),
}

# extra key -> v2 variable, per kind (STS and ASC share some extra keys).
V2_EXTRA_KEY_MAP: dict[Kind, dict[str, str]] = {
    Kind.STS: {
        "gantry_x_m": "GantryPos_m", "trolley_pos_m": "TrolleyPos_m", "hoist_pos_m": "HoistPos_m",
        "lane_id": "LaneId", "holding": "Holding", "lane_buffer": "LaneBuffer",
        "lane_buffer_count": "LaneBufferCount", "wait_for_agv_s": "WaitForAgv_s",
        "wait_for_agv_total_s": "WaitForAgvTotal_s",
    },
    Kind.AGV: {
        "heading_deg": "Heading_deg", "speed_mps": "Speed_mps", "load_a": "LoadA",
        "load_b": "LoadB", "charge_pct": "ChargePct", "at_point": "AtPoint",
    },
    Kind.ASC: {
        "bay": "Bay", "row": "Row", "tier": "Tier", "trolley_pos_m": "TrolleyPos_m",
        "hoist_pos_m": "HoistPos_m", "partner": "Partner", "role": "Role", "holding": "Holding",
    },
}

# Derived from ``State`` - a display convention, not sim facts: the sim has no manual/remote
# mode and no warning levels of its own. MAINTENANCE -> YELLOW / "Maintenance" and
# FAULT -> RED / "Automatic" (a faulted crane stays in automatic mode) are our mapping onto the
# Konecranes screens. OFFLINE (no data; the gateway reports a lost link so and also writes these
# two on the mirror) -> RED / "Offline".
TECHNICAL_STATUS = {"IDLE": "GREEN", "BUSY": "GREEN", "MAINTENANCE": "YELLOW", "FAULT": "RED",
                    "OFFLINE": "RED"}
OPERATIONAL_MODE = {"IDLE": "Automatic", "BUSY": "Automatic", "FAULT": "Automatic",
                    "MAINTENANCE": "Maintenance", "OFFLINE": "Offline"}

# Transfer zone (one object per block under ``Terminal/TransferZones``, browse name = block id,
# display name ``ICZ<block>-WS``) with a ``Racks`` folder of ``RackType`` objects (browse name =
# rack id). ``ContainerId`` "" = free.
# ``LastDataReceivedAt`` (UTC DateTime) on a zone: when the sim server last wrote a changed rack
# occupancy (also at start), mirrored by the gateway as is.
ZONE_VARS = ("BlockId", "RackCount", "RacksOccupied", "Online", "LastDataReceivedAt")
RACK_VARS = ("Index", "ContainerId", "Reefer", "Size")

_S, _D, _B = ua.VariantType.String, ua.VariantType.Double, ua.VariantType.Boolean
_I = ua.VariantType.Int32
_T = ua.VariantType.DateTime

VAR_TYPES: dict[str, ua.VariantType] = {
    "Id": _S, "Kind": _S, "Vendor": _S, "State": _S,
    "PositionX": _D, "PositionY": _D, "CurrentJobId": _S, "EnergyKwh": _D,
    "Extra": _S, "LastJobId": _S, "LastJobOutcome": _S,
    "BerthSide": _S, "SpreaderHeightM": _D, "TrolleyPositionM": _D,
    "HandoverLaneOccupied": _B,
    "BatteryPct": _D, "LoadedContainerId": _S, "Route": _S, "SpeedMps": _D,
    "BlockId": _S, "GantryPositionM": _D, "WsTpOccupied": _B, "LsTpOccupied": _B,
    # v2
    "TechnicalStatus": _S, "OperationalMode": _S, "LastDataReceivedAt": _D,
    "GantryPos_m": _D, "TrolleyPos_m": _D, "HoistPos_m": _D, "LaneId": _S, "Holding": _S,
    "LaneBuffer": _S, "LaneBufferCount": _I, "WaitForAgv_s": _D, "WaitForAgvTotal_s": _D,
    "Heading_deg": _D, "Speed_mps": _D, "LoadA": _S, "LoadB": _S, "ChargePct": _D, "AtPoint": _S,
    "Bay": _D, "Row": _I, "Tier": _I, "Partner": _S, "Role": _S,
    # transfer zones
    "RackCount": _I, "RacksOccupied": _I, "Online": _B,
    "Index": _I, "ContainerId": _S, "Reefer": _B, "Size": _S,
}

_DEFAULTS = {_S: "", _D: 0.0, _B: False, _I: 0,
             _T: datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)}

# Variables whose type on a TransferZone differs from VAR_TYPES (same browse name, other type).
ZONE_VAR_TYPES: dict[str, ua.VariantType] = {"LastDataReceivedAt": _T}


@dataclass
class Types:
    equipment: ua.NodeId
    sts: ua.NodeId
    agv: ua.NodeId
    asc: ua.NodeId
    control: ua.NodeId
    events: EventTypes
    # v2 only (``register_types(v2=True)``), else None
    sts2: ua.NodeId | None = None
    agv2: ua.NodeId | None = None
    asc2: ua.NodeId | None = None
    zone: ua.NodeId | None = None
    rack: ua.NodeId | None = None

    def equipment_type(self, kind: Kind, *, v2: bool = False) -> ua.NodeId:
        if v2:
            t = {Kind.STS: self.sts2, Kind.AGV: self.agv2, Kind.ASC: self.asc2}[kind]
            if t is None:
                raise ValueError("v2 object types not registered (register_types(v2=True))")
            return t
        return {Kind.STS: self.sts, Kind.AGV: self.agv, Kind.ASC: self.asc}[kind]


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


def to_variant(name: str, value: object, vt: ua.VariantType | None = None) -> ua.Variant:
    vt = vt or VAR_TYPES[name]
    if vt is _S:
        value = "" if value is None else str(value)
    elif vt is _D:
        value = float(value)  # type: ignore[arg-type]
    elif vt is _B:
        value = bool(value)
    elif vt is _I:
        # -1: no value (e.g. an ASC's last Tier before its first pick; positions are 0-based)
        value = -1 if value is None else int(value)  # type: ignore[call-overload]
    return ua.Variant(value, vt)


def zone_variant(name: str, value: object) -> ua.Variant:
    """``to_variant`` for a variable of a TransferZone object."""
    return to_variant(name, value, ZONE_VAR_TYPES.get(name))


async def _add_var(parent: Node, idx: int, name: str, vt: ua.VariantType | None = None) -> None:
    vt = vt or VAR_TYPES[name]
    v = await parent.add_variable(idx, name, ua.Variant(_DEFAULTS[vt], vt))
    await v.set_modelling_rule(True)


async def register_types(server: Server, *, v2: bool = False) -> tuple[int, Types]:
    """Register the namespace and its types. ``v2`` adds the C1b types (``STS2Type``,
    ``AGV2Type``, ``ASC2Type``, ``TransferZoneType``, ``RackType``) after all v1 types, so the
    v1 part of the address space is the same either way."""
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
    events = await register_event_types(server, idx)
    types = Types(
        equipment=equipment.nodeid,
        sts=kind_nodes[Kind.STS].nodeid,
        agv=kind_nodes[Kind.AGV].nodeid,
        asc=kind_nodes[Kind.ASC].nodeid,
        control=control.nodeid,
        events=events,
    )
    if v2:
        v2_nodes: dict[Kind, Node] = {}
        for kind, name in ((Kind.STS, "STS2Type"), (Kind.AGV, "AGV2Type"), (Kind.ASC, "ASC2Type")):
            t = await kind_nodes[kind].add_object_type(idx, name)
            for var in V2_KIND_VARS[kind]:
                await _add_var(t, idx, var)
            v2_nodes[kind] = t
        zone = await base.add_object_type(idx, "TransferZoneType")
        for var in ZONE_VARS:
            await _add_var(zone, idx, var, ZONE_VAR_TYPES.get(var))
        rack = await base.add_object_type(idx, "RackType")
        for var in RACK_VARS:
            await _add_var(rack, idx, var)
        types.sts2 = v2_nodes[Kind.STS].nodeid
        types.agv2 = v2_nodes[Kind.AGV].nodeid
        types.asc2 = v2_nodes[Kind.ASC].nodeid
        types.zone = zone.nodeid
        types.rack = rack.nodeid
    return idx, types


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
    snap: EquipmentSnapshot, handlers: EquipmentHandlers, *, v2: bool = False,
) -> Node:
    """An equipment object of the kind's type, or of its v2 subtype (``v2``)."""
    type_id = types.equipment_type(snap.kind, v2=v2)
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
    await write_snapshot(obj, snap, v2=v2)
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


async def zones_folder(server: Server, idx: int, terminal: Node) -> Node:
    """``Terminal/TransferZones`` (v2 only; outside ``Yard/Block-*``, whose objects are ASCs)."""
    return await _child_or_create(terminal, idx, "TransferZones")


def zone_display_name(block: str) -> str:
    """Konecranes naming of a waterside interchange zone: ``ICZ<block>-WS``."""
    return f"ICZ{block}-WS"


async def create_zone_object(
    server: Server, idx: int, types: Types, parent: Node, block: str,
    racks: Sequence[tuple[str, bool, str]],
) -> Node:
    """A ``TransferZoneType`` object ``<block>`` with ``Racks/<rack id>`` (``RackType``) per
    ``(rack_id, reefer, size)`` in zone order (``Index`` 0..), all free; ``Online`` False."""
    if types.zone is None or types.rack is None:
        raise ValueError("v2 object types not registered (register_types(v2=True))")
    obj = (await instantiate(parent, server.get_node(types.zone), bname=f"{idx}:{block}", idx=idx))[0]
    await obj.write_attribute(
        ua.AttributeIds.DisplayName, ua.DataValue(ua.Variant(
            ua.LocalizedText(zone_display_name(block)), ua.VariantType.LocalizedText)))
    folder = await obj.add_folder(idx, "Racks")
    for i, (rack_id, reefer, size) in enumerate(racks):
        rack = (await instantiate(
            folder, server.get_node(types.rack), bname=f"{idx}:{rack_id}", idx=idx))[0]
        await _write(rack, "Index", i)
        await _write(rack, "Reefer", reefer)
        await _write(rack, "Size", size)
    await _write(obj, "BlockId", block)
    await _write(obj, "RackCount", len(racks))
    return obj


async def zone_rack_vars(zone: Node) -> dict[str, dict[str, Node]]:
    """``{rack_id: {var name: node}}`` of a zone object, in ``Index`` order (cached per object)."""
    cache = _VAR_CACHE.setdefault(zone.session, {}).setdefault(zone.nodeid, {})
    found = cache.get("__racks__")
    if found is not None:
        return found
    folder = await get_var(zone, "Racks")
    racks: list[tuple[int, str, dict[str, Node]]] = []
    for rack in await folder.get_children():
        name = (await rack.read_browse_name()).Name
        nodes = {v: await get_var(rack, v) for v in RACK_VARS}
        racks.append((int(await nodes["Index"].read_value()), name, nodes))
    out = {name: nodes for _, name, nodes in sorted(racks, key=lambda r: r[0])}
    cache["__racks__"] = out
    return out


async def write_zone(
    zone: Node, occupancy: Mapping[str, str | None], *, online: bool = True,
    changed_at: datetime.datetime | None = None,
) -> None:
    """Write ``ContainerId`` of every rack in ``occupancy`` (None -> ""), ``RacksOccupied``,
    ``Online`` and, when given, ``LastDataReceivedAt`` (``changed_at``)."""
    racks = await zone_rack_vars(zone)
    for rack_id, cid in occupancy.items():
        await racks[rack_id]["ContainerId"].write_value(to_variant("ContainerId", cid))
    await _write(zone, "RacksOccupied", sum(c is not None for c in occupancy.values()))
    await _write(zone, "Online", online)
    if changed_at is not None:
        await (await get_var(zone, "LastDataReceivedAt")).write_value(
            zone_variant("LastDataReceivedAt", changed_at))


async def block_folder(server: Server, idx: int, yard: Node, block: str) -> Node:
    return await _child_or_create(yard, idx, f"Block-{block}")


# NodeIds repeat across servers (numeric ids per namespace), so per-node state
# is scoped by the node's session (one per server) as well as by NodeId.
_VAR_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


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


def v2_values(snap: EquipmentSnapshot, *, sim_time: float | None = None) -> dict[str, object]:
    """The v2 variables of ``snap``: typed views of its extra (``Route``-like containers as
    JSON), ``TechnicalStatus``/``OperationalMode`` from the state and ``LastDataReceivedAt``
    (sim seconds of the write; left out when ``sim_time`` is None)."""
    state = snap.state.value
    out: dict[str, object] = {
        "TechnicalStatus": TECHNICAL_STATUS.get(state, ""),
        "OperationalMode": OPERATIONAL_MODE.get(state, ""),
    }
    if sim_time is not None:
        out["LastDataReceivedAt"] = sim_time
    for key, var in V2_EXTRA_KEY_MAP[snap.kind].items():
        if key not in snap.extra:
            continue
        value = snap.extra[key]
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value)
        out[var] = value
    return out


async def write_snapshot(
    node: Node, snap: EquipmentSnapshot, *, v2: bool = False, sim_time: float | None = None,
) -> None:
    await _write(node, "Id", snap.equipment_id)
    await _write(node, "Kind", snap.kind.value)
    await _write(node, "Vendor", snap.vendor)
    await _write(node, "State", snap.state.value)
    await _write(node, "PositionX", snap.position.x)
    await _write(node, "PositionY", snap.position.y)
    await _write(node, "CurrentJobId", snap.current_job_id)
    await _write(node, "EnergyKwh", snap.energy_kwh)
    await _write(node, "Extra", json.dumps(dict(snap.extra)))
    await _write(node, "LastJobId", snap.extra.get("last_job_id"))
    await _write(node, "LastJobOutcome", snap.extra.get("last_job_outcome"))
    allowed = set(KIND_VARS[snap.kind])
    for key, value in snap.extra.items():
        var = EXTRA_KEY_MAP.get(key)
        if var is None or var not in allowed:
            continue
        if var == "Route":
            value = json.dumps(value)
        await _write(node, var, value)
    if v2:
        for var, value in v2_values(snap, sim_time=sim_time).items():
            await _write(node, var, value)

