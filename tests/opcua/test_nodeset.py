import asyncio
import json
import socket
import threading

import pytest
from asyncua import Client, Server

from terminal_model.port import EquipmentSnapshot
from terminal_model.types import Kind, Position, State
from terminal_opcua import nodeset


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server():
    port = _free_port()
    srv = Server()
    await srv.init()
    srv.set_endpoint(f"opc.tcp://127.0.0.1:{port}")
    srv.url = f"opc.tcp://127.0.0.1:{port}"
    async with srv:
        yield srv


def _agv_snap(**extra):
    return EquipmentSnapshot(
        equipment_id="AGV-01", kind=Kind.AGV, vendor="V", state=State.IDLE,
        position=Position(1.0, 2.0), current_job_id=None, energy_kwh=3.5,
        extra=extra,
    )


async def test_types_registered(server):
    idx, types = await nodeset.register_types(server)
    assert idx > 1
    eq = server.get_node(types.equipment)
    names = {(await c.read_browse_name()).Name for c in await eq.get_children()}
    assert set(nodeset.EQUIPMENT_VARS) <= names
    agv = server.get_node(types.agv)
    names = {(await c.read_browse_name()).Name for c in await agv.get_children()}
    assert "BatteryPct" in names
    for n in (types.sts, types.asc, types.control):
        assert await server.get_node(n).read_browse_name()
    ctl = server.get_node(types.control)
    names = {(await c.read_browse_name()).Name for c in await ctl.get_children()}
    assert {"Stop", "PrefillYard", "Start"} <= names
    base = server.get_node(types.events.base)
    assert (await base.read_browse_name()).Name == "TerminalEventType"
    assert not {"LastEvent", "EventSeq"} & set(nodeset.EQUIPMENT_VARS)


async def test_create_agv_object_and_read(server):
    idx, types = await nodeset.register_types(server)
    folders = await nodeset.ensure_folders(server, idx, server.nodes.objects)
    h = nodeset.EquipmentHandlers(lambda j: True, lambda: True, lambda: True)
    snap = _agv_snap(battery_pct=80.0, route=[[1, 2]], unknown_key=5)
    obj = await nodeset.create_equipment_object(
        server, idx, types, folders["Fleet"], snap, h)
    bp = await obj.get_child(f"{idx}:BatteryPct")
    assert await bp.read_value() == 80.0
    assert await (await obj.get_child(f"{idx}:Route")).read_value() == "[[1, 2]]"
    extra = json.loads(await (await obj.get_child(f"{idx}:Extra")).read_value())
    assert extra["unknown_key"] == 5
    assert await (await obj.get_child(f"{idx}:State")).read_value() == "IDLE"
    names = {(await c.read_browse_name()).Name for c in await obj.get_children()}
    assert not {"LastEvent", "EventSeq"} & names


async def test_method_calls_handlers(server):
    idx, types = await nodeset.register_types(server)
    folders = await nodeset.ensure_folders(server, idx, server.nodes.objects)
    calls = []

    def assign(s):
        calls.append(s)
        return False

    h = nodeset.EquipmentHandlers(assign, lambda: True, lambda: True)
    await nodeset.create_equipment_object(
        server, idx, types, folders["Fleet"], _agv_snap(), h)
    async with Client(server.url) as c:
        o = await c.nodes.objects.get_child(
            [f"{idx}:Terminal", f"{idx}:Fleet", f"{idx}:AGV-01"])
        assert await o.call_method(f"{idx}:AssignJob", "{}") is False
        assert await o.call_method(f"{idx}:Reset") is True
    assert calls == ["{}"]


async def test_handlers_run_on_loop_thread(server):
    idx, types = await nodeset.register_types(server)
    folders = await nodeset.ensure_folders(server, idx, server.nodes.objects)
    idents = []

    def rec(*_a):
        idents.append(threading.get_ident())
        return True

    h = nodeset.EquipmentHandlers(rec, rec, rec)
    await nodeset.create_equipment_object(
        server, idx, types, folders["Fleet"], _agv_snap(), h)
    async with Client(server.url) as c:
        o = await c.nodes.objects.get_child(
            [f"{idx}:Terminal", f"{idx}:Fleet", f"{idx}:AGV-01"])
        await o.call_method(f"{idx}:AssignJob", "{}")
        await o.call_method(f"{idx}:CancelJob")
        await o.call_method(f"{idx}:Reset")
    assert idents == [threading.get_ident()] * 3


async def test_create_control_object(server):
    idx, types = await nodeset.register_types(server)
    folders = await nodeset.ensure_folders(server, idx, server.nodes.objects)
    stopped, prefilled, started = [], [], []

    def prefill(s):
        prefilled.append(s)
        return False

    def start():
        started.append(1)
        return True

    await nodeset.create_control_object(
        server, idx, types, folders["Terminal"],
        stop=lambda: stopped.append(1), prefill=prefill, start=start)
    async with Client(server.url) as c:
        o = await c.nodes.objects.get_child([f"{idx}:Terminal", f"{idx}:Clock"])
        assert (await o.read_browse_name()).Name == "Clock"
        await o.call_method(f"{idx}:Stop")
        assert await o.call_method(f"{idx}:PrefillYard", "[]") is False
        assert await o.call_method(f"{idx}:Start") is True
    assert stopped == [1] and prefilled == ["[]"] and started == [1]


async def test_async_handlers_are_awaited(server):
    idx, types = await nodeset.register_types(server)
    folders = await nodeset.ensure_folders(server, idx, server.nodes.objects)
    calls = []

    async def assign(s):
        await asyncio.sleep(0)
        calls.append(s)
        return True

    async def nope():
        return False

    h = nodeset.EquipmentHandlers(assign, nope, nope)
    await nodeset.create_equipment_object(
        server, idx, types, folders["Fleet"], _agv_snap(), h)

    async def stop():
        calls.append("stop")

    async def prefill(s):
        return True

    async def start():
        calls.append("start")
        return False

    await nodeset.create_control_object(
        server, idx, types, folders["Terminal"], stop=stop, prefill=prefill, start=start)
    async with Client(server.url) as c:
        o = await c.nodes.objects.get_child(
            [f"{idx}:Terminal", f"{idx}:Fleet", f"{idx}:AGV-01"])
        assert await o.call_method(f"{idx}:AssignJob", "{}") is True
        assert await o.call_method(f"{idx}:Reset") is False
        clk = await c.nodes.objects.get_child([f"{idx}:Terminal", f"{idx}:Clock"])
        await clk.call_method(f"{idx}:Stop")
        assert await clk.call_method(f"{idx}:PrefillYard", "[]") is True
        assert await clk.call_method(f"{idx}:Start") is False
    assert calls == ["{}", "stop", "start"]
