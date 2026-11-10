import asyncio
import random
import socket

import pytest
import salabim
from asyncua import Client

from terminal_model.port import EventKind
from terminal_model.types import Job, JobType, Kind, Position
from terminal_opcua.codec import event_from_json, job_to_json
from terminal_sim.equipment.sts import STS
from terminal_sim.grid import Grid
from terminal_sim.opcua_servers import EquipmentServer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _job(job_id="J1"):
    return Job(job_id, JobType.DISCHARGE, "C1", "SHIP", "LANE")


class Rig:
    pass


@pytest.fixture
async def rig():
    r = Rig()
    r.env = salabim.Environment(trace=False, yieldless=False)
    r.grid = Grid(20, 6, 20.0)
    r.server = None
    port = _free_port()

    def make_server(eq):
        r.server = EquipmentServer(eq, port=port, host="127.0.0.1")
        return r.server

    # Equipment needs `emit` at construction; forward lazily to the server.
    r.sts = STS(
        equipment_id="STS-01", kind=Kind.STS, position=Position(0.0, 0.0),
        rng=random.Random(1), emit=lambda ev: r.server.on_emit(ev), env=r.env,
        lane_cells=((4, 0), (5, 0)), grid=r.grid,
    )
    make_server(r.sts)
    await r.server.start()
    r.client = Client(r.server.endpoint)
    await r.client.connect()
    idx = await r.client.get_namespace_index("urn:terminal:model")
    r.idx = idx
    r.obj = await r.client.nodes.objects.get_child(
        [f"{idx}:Terminal", f"{idx}:Quay", f"{idx}:STS-01"])
    yield r
    await r.client.disconnect()
    await r.server.stop()


async def _val(r, name):
    return await (await r.obj.get_child(f"{r.idx}:{name}")).read_value()


async def test_assign_via_opcua_runs_job(rig):
    ok = await rig.obj.call_method(f"{rig.idx}:AssignJob", job_to_json(_job()))
    assert ok is True
    rig.grid.try_reserve((4, 0), "AGV-01")
    rig.env.run(300)
    await rig.server.sync()
    assert await _val(rig, "EventSeq") >= 2
    assert await _val(rig, "State") == "IDLE"
    assert await _val(rig, "EnergyKwh") == 5.0


async def test_two_events_one_step_both_delivered(rig):
    assert rig.sts.assign_job(_job())
    await rig.server.sync()
    base = await _val(rig, "EventSeq")

    changes = []

    class H:
        def datachange_notification(self, node, val, data):
            changes.append(val)

    seq_node = await rig.obj.get_child(f"{rig.idx}:EventSeq")
    sub = await rig.client.create_subscription(10, H())
    await sub.subscribe_data_change(seq_node)
    await asyncio.sleep(0.1)
    changes.clear()

    rig.sts.cancel_job()   # JOB_FAILED
    rig.sts.reset()        # STATE_CHANGED
    await rig.server.sync()
    await asyncio.sleep(0.3)
    assert await _val(rig, "EventSeq") == base + 2
    assert changes == [base + 1, base + 2]
    ev = event_from_json(await _val(rig, "LastEvent"))
    assert ev.kind == EventKind.STATE_CHANGED
    await sub.delete()


async def test_events_delivered_in_order_via_last_event(rig):
    assert rig.sts.assign_job(_job())
    await rig.server.sync()
    lastevents = []

    class H:
        def datachange_notification(self, node, val, data):
            lastevents.append(event_from_json(val).kind)

    node = await rig.obj.get_child(f"{rig.idx}:LastEvent")
    sub = await rig.client.create_subscription(10, H())
    await sub.subscribe_data_change(node)
    await asyncio.sleep(0.1)
    lastevents.clear()
    rig.sts.cancel_job()
    rig.sts.reset()
    await rig.server.sync()
    await asyncio.sleep(0.3)
    assert lastevents == [EventKind.JOB_FAILED, EventKind.STATE_CHANGED]
    await sub.delete()


async def test_invalid_json_returns_false(rig):
    assert await rig.obj.call_method(f"{rig.idx}:AssignJob", '{"job_id": 1}') is False
    assert await rig.obj.call_method(f"{rig.idx}:AssignJob", "not json") is False
    # server still alive
    assert await _val(rig, "Id") == "STS-01"


async def test_wrong_state_returns_false(rig):
    assert await rig.obj.call_method(f"{rig.idx}:AssignJob", job_to_json(_job())) is True
    assert await rig.obj.call_method(
        f"{rig.idx}:AssignJob", job_to_json(_job("J2"))) is False
