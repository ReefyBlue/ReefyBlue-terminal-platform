"""TerminalEventType hierarchy, EventEmitter and client-side decoding over a real server."""

import asyncio
import socket
from types import SimpleNamespace

import pytest
from asyncua import Client, Server, ua
from event_sink import EventSink
from terminal_model.port import EquipmentEvent, EquipmentSnapshot, EventKind
from terminal_model.types import Kind, Position, State
from terminal_opcua import nodeset
from terminal_opcua.events import (
    EventEmitter,
    event_from_ua,
    message_for,
    resolve_event_types,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server_client():
    port = _free_port()
    srv = Server()
    await srv.init()
    srv.set_endpoint(f"opc.tcp://127.0.0.1:{port}")
    srv.url = f"opc.tcp://127.0.0.1:{port}"
    idx, types = await nodeset.register_types(srv)
    async with srv, Client(srv.url) as client:
        yield srv, client, idx, types


@pytest.fixture
async def agv_obj(server_client):
    server, _client, idx, types = server_client
    folders = await nodeset.ensure_folders(server, idx, server.nodes.objects)
    h = nodeset.EquipmentHandlers(lambda j: True, lambda: True, lambda: True)
    snap = EquipmentSnapshot(
        equipment_id="AGV-01", kind=Kind.AGV, vendor="V", state=State.IDLE,
        position=Position(1.0, 2.0), current_job_id=None, energy_kwh=3.5, extra={},
    )
    return await nodeset.create_equipment_object(
        server, idx, types, folders["Fleet"], snap, h)


async def _setup(server_client, agv_obj):
    server, client, _idx, types = server_client
    sink = EventSink()
    await sink.attach(client)
    em = await EventEmitter.create(server, types.events, agv_obj, "AGV-01")
    return sink, em


async def test_event_types_registered(server_client):
    _server, client, _idx, types = server_client
    types = types.events
    r = await resolve_event_types(client)
    assert r == types
    base = client.get_node(types.base)
    names = {(await p.read_browse_name()).Name for p in await base.get_properties()}
    assert {"EquipmentId", "Seq", "SimTime", "JobId", "Reason", "NewState"} <= names
    for sub in (types.state_changed, types.job_completed, types.job_failed):
        parents = await client.get_node(sub).get_referenced_nodes(
            refs=ua.ObjectIds.HasSubtype, direction=ua.BrowseDirection.Inverse)
        assert parents[0].nodeid == types.base


async def test_three_kinds_received_with_seq(server_client, agv_obj):
    sink, em = await _setup(server_client, agv_obj)
    ev1 = EquipmentEvent(EventKind.STATE_CHANGED, "AGV-01", 1.5, new_state=State.BUSY)
    ev2 = EquipmentEvent(EventKind.JOB_COMPLETED, "AGV-01", 2.0, job_id="J1")
    ev3 = EquipmentEvent(EventKind.JOB_FAILED, "AGV-01", 3.25, job_id="J1", reason="X")
    for ev in (ev1, ev2, ev3):
        await em.emit(ev)
    assert await sink.wait_for(3, 2.0)
    assert [s for s, _ in sink.received] == [1, 2, 3]
    assert sink.events() == [ev1, ev2, ev3]
    assert sink.unknown == []
    assert em.seq == 3
    await sink.detach()


async def test_source_node_is_equipment_object(server_client, agv_obj):
    sink, em = await _setup(server_client, agv_obj)
    ev = EquipmentEvent(EventKind.JOB_COMPLETED, "AGV-01", 2.0, job_id="J1")
    await em.emit(ev)
    assert await sink.wait_for(1, 2.0)
    raw = sink.raw[0]
    assert raw.SourceNode == agv_obj.nodeid
    assert raw.SourceName == "AGV-01"
    assert raw.Message.Text == message_for(ev) == "JOB_COMPLETED AGV-01 job=J1"
    await sink.detach()


async def test_severity_500_for_failure_and_fault(server_client, agv_obj):
    sink, em = await _setup(server_client, agv_obj)
    await em.emit(EquipmentEvent(EventKind.JOB_FAILED, "AGV-01", 1.0, job_id="J1", reason="X"))
    await em.emit(EquipmentEvent(EventKind.STATE_CHANGED, "AGV-01", 2.0, new_state=State.FAULT))
    await em.emit(EquipmentEvent(EventKind.STATE_CHANGED, "AGV-01", 3.0, new_state=State.IDLE))
    assert await sink.wait_for(3, 2.0)
    assert [r.Severity for r in sink.raw] == [500, 500, 100]
    await sink.detach()


async def test_concurrent_emits_no_lost_seq(server_client, agv_obj):
    sink, em = await _setup(server_client, agv_obj)
    evs = [EquipmentEvent(EventKind.JOB_COMPLETED, "AGV-01", float(i), job_id=f"J{i}")
           for i in range(20)]
    await asyncio.gather(*(em.emit(e) for e in evs))
    assert await sink.wait_for(20, 2.0)
    assert sorted(s for s, _ in sink.received) == list(range(1, 21))
    assert sorted(e.sim_time for e in sink.events()) == [float(i) for i in range(20)]
    await sink.detach()


async def test_kind_of_unknown_raises(server_client):
    types = server_client[3].events
    assert types.kind_of(types.job_failed) is EventKind.JOB_FAILED
    with pytest.raises(ValueError):
        types.kind_of(ua.NodeId(ua.ObjectIds.SystemEventType))


async def test_message_for_formats_all_parts():
    ev = EquipmentEvent(EventKind.STATE_CHANGED, "STS-01", 0.0, new_state=State.FAULT,
                        reason="lost link")
    assert message_for(ev) == "STATE_CHANGED STS-01 reason=lost link state=FAULT"


async def test_event_from_ua_decode_failures_are_value_errors(server_client):
    types = server_client[3].events
    good = {"EventType": types.job_completed, "EquipmentId": "AGV-01", "Seq": 7,
            "SimTime": 1.0, "JobId": "J1", "Reason": "", "NewState": ""}
    assert event_from_ua(SimpleNamespace(**good), types) == (
        7, EquipmentEvent(EventKind.JOB_COMPLETED, "AGV-01", 1.0, job_id="J1"))
    bad = [
        {**good, "EventType": ua.NodeId(ua.ObjectIds.BaseEventType)},  # unknown type
        {k: v for k, v in good.items() if k != "Reason"},  # AttributeError
        {**good, "SimTime": None},  # TypeError
        {**good, "Seq": None},  # TypeError
        {**good, "NewState": "BOGUS"},  # ValueError from State()
    ]
    for fields in bad:
        with pytest.raises(ValueError):
            event_from_ua(SimpleNamespace(**fields), types)
