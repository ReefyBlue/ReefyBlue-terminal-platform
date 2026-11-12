"""C1b v2 OPC UA variables: the v2 object types (``STS2Type``/``AGV2Type``/``ASC2Type``), the
``TransferZone`` objects of the sim's zone servers, and that a legacy address space is unchanged."""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from pathlib import Path

import pytest
from asyncua import Client, Server, ua
from terminal_model.port import EquipmentSnapshot
from terminal_model.types import Kind, Position, State
from terminal_opcua import nodeset
from terminal_sim.layout import load_layout
from terminal_sim.opcua_servers import EquipmentServer, ZoneServer
from terminal_sim.scenario import load_scenario
from terminal_sim.world import make_world

ROOT = Path(__file__).resolve().parents[2]
NS = nodeset.NAMESPACE_URI


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _world2():
    layout = load_layout(ROOT / "terminal_mv_small.yaml")
    scenario = replace(load_scenario(ROOT / "tests/e2e/fixtures/mv_small_baseline.yaml", layout),
                       n_agv=2)
    return make_world(layout, scenario)


async def _subtree_names(server: Server, node) -> set[str]:
    out: set[str] = set()
    stack = [node]
    while stack:
        n = stack.pop()
        for c in await n.get_children(refs=ua.ObjectIds.HierarchicalReferences):
            out.add((await c.read_browse_name()).Name)
            if (await c.read_node_class()) == ua.NodeClass.ObjectType:
                stack.append(c)
    return out


async def _server() -> Server:
    srv = Server()
    await srv.init()
    srv.set_endpoint(f"opc.tcp://127.0.0.1:{_free_port()}")
    return srv


async def test_v1_types_unchanged_and_v2_types_only_on_request():
    v1, v2 = await _server(), await _server()
    _, t1 = await nodeset.register_types(v1)
    _, t2 = await nodeset.register_types(v2, v2=True)
    # v2 types are registered after every v1 type: the v1 NodeIds are the same
    for f in ("equipment", "sts", "agv", "asc", "control"):
        assert getattr(t1, f) == getattr(t2, f)
    assert t1.events == t2.events
    assert (t1.sts2, t1.agv2, t1.asc2, t1.zone, t1.rack) == (None,) * 5
    names1 = await _subtree_names(v1, v1.nodes.base_object_type)
    names2 = await _subtree_names(v2, v2.nodes.base_object_type)
    v2_types = {"STS2Type", "AGV2Type", "ASC2Type", "TransferZoneType", "RackType"}
    assert not v2_types & names1
    assert v2_types <= names2
    with pytest.raises(ValueError):
        t1.equipment_type(Kind.STS, v2=True)


def test_v2_values_derived_and_typed():
    snap = EquipmentSnapshot(
        equipment_id="STS-01", kind=Kind.STS, vendor="V", state=State.FAULT,
        position=Position(0, 0), current_job_id=None, energy_kwh=0.0,
        extra={"lane_buffer": {"L1": "C1", "L2": None}, "holding": None, "trolley_pos_m": 3.0})
    v = nodeset.v2_values(snap, sim_time=12.5)
    assert v["TechnicalStatus"] == "RED" and v["OperationalMode"] == "Automatic"
    assert v["LastDataReceivedAt"] == 12.5
    assert json.loads(v["LaneBuffer"]) == {"L1": "C1", "L2": None}
    assert v["Holding"] is None and v["TrolleyPos_m"] == 3.0
    assert "LaneId" not in v  # not in extra: left at its last value
    off = nodeset.v2_values(replace(snap, state=State.OFFLINE))
    assert (off["TechnicalStatus"], off["OperationalMode"]) == ("", "")
    assert "LastDataReceivedAt" not in off
    assert nodeset.v2_values(replace(snap, state=State.MAINTENANCE))["TechnicalStatus"] == "YELLOW"


async def _read_object(client: Client, path: list[str]) -> dict[str, object]:
    idx = await client.get_namespace_index(NS)
    obj = await client.nodes.objects.get_child([f"{idx}:{p}" for p in path])
    out = {}
    for c in await obj.get_children(nodeclassmask=ua.NodeClass.Variable):
        out[(await c.read_browse_name()).Name] = await c.read_value()
    return out


@pytest.mark.parametrize(("eid", "path"), [
    ("STS-01", ["Terminal", "Quay", "STS-01"]),
    ("AGV-01", ["Terminal", "Fleet", "AGV-01"]),
    ("ASC-A-WS", ["Terminal", "Yard", "Block-A", "ASC-A-WS"]),
])
async def test_v2_equipment_server_serves_v2_vars(eid, path):
    world = _world2()
    world.run(until=5.0)
    eq = world.equipment[eid]
    server = EquipmentServer(eq, port=_free_port(), host="127.0.0.1", v2=True)
    await server.start()
    try:
        await server.sync()
        async with Client(server.endpoint) as client:
            got = await _read_object(client, path)
    finally:
        await server.stop()
    snap = eq.snapshot()
    kind = snap.kind
    assert set(nodeset.V2_KIND_VARS[kind]) <= set(got)
    assert got["LastDataReceivedAt"] == pytest.approx(5.0)
    assert got["TechnicalStatus"] == "GREEN" and got["OperationalMode"] == "Automatic"
    for key, var in nodeset.V2_EXTRA_KEY_MAP[kind].items():
        value = snap.extra[key]  # every mapped key is reported by the v2 sim
        if isinstance(value, dict | list):
            assert json.loads(got[var]) == value, var
        elif value is None:
            assert got[var] == ("" if isinstance(got[var], str) else -1), var
        elif isinstance(value, float):
            assert got[var] == pytest.approx(value), var
        else:
            assert got[var] == value, var
    assert json.loads(got["Extra"]) == json.loads(json.dumps(dict(snap.extra)))


async def test_legacy_equipment_server_has_no_v2_vars():
    layout = load_layout(ROOT / "terminal.yaml")
    world = make_world(layout, replace(load_scenario(ROOT / "scenarios/baseline.yaml"), n_agv=1))
    server = EquipmentServer(world.equipment["STS-01"], port=_free_port(), host="127.0.0.1")
    await server.start()
    try:
        await server.sync()
        async with Client(server.endpoint) as client:
            got = await _read_object(client, ["Terminal", "Quay", "STS-01"])
    finally:
        await server.stop()
    assert set(got) == {*nodeset.EQUIPMENT_VARS, *nodeset.KIND_VARS[Kind.STS]}


async def test_zone_server_racks():
    world = _world2()
    zone = world.terminal.transfer_zone("A")
    server = ZoneServer(zone, world.racks, port=_free_port(), host="127.0.0.1")
    await server.start()
    try:
        first, reefer = zone.racks[0], next(r for r in zone.racks if r.reefer)
        world.racks.put(first.id, "C-1")
        await server.sync()
        async with Client(server.endpoint) as client:
            idx = await client.get_namespace_index(NS)
            obj = await client.nodes.objects.get_child(
                [f"{idx}:Terminal", f"{idx}:TransferZones", f"{idx}:A"])
            assert (await obj.read_display_name()).Text == "ICZA-WS"
            top = await _read_object(client, ["Terminal", "TransferZones", "A"])
            assert top == {"BlockId": "A", "RackCount": len(zone.racks), "RacksOccupied": 1,
                           "Online": True}
            racks = {}
            for r in zone.racks:
                racks[r.id] = await _read_object(client, ["Terminal", "TransferZones", "A", "Racks", r.id])
        assert [racks[r.id]["Index"] for r in zone.racks] == list(range(len(zone.racks)))
        assert racks[first.id]["ContainerId"] == "C-1"
        assert all(racks[r.id]["ContainerId"] == "" for r in zone.racks[1:])
        assert racks[reefer.id]["Reefer"] is True and racks[first.id]["Reefer"] is False
        assert racks[first.id]["Size"] == first.size
    finally:
        await server.stop()
