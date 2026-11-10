import asyncio
import random
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import salabim
from asyncua import Client
from terminal_gateway.gateway import Gateway
from terminal_model.port import EventKind
from terminal_model.types import Kind, Position, State
from terminal_opcua.codec import event_from_json
from terminal_sim.equipment.sts import STS
from terminal_sim.grid import Grid
from terminal_sim.layout import load_layout
from terminal_sim.opcua_servers import ControlServer, EquipmentServer
from terminal_sim.yard import Yard

ROOT = Path(__file__).resolve().parents[2]
NS = "urn:terminal:model"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _free_base_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] % 20000 + 20000  # base..base+12 stay below 40000


async def _wait_for(pred, timeout: float, step: float = 0.01):
    deadline = time.monotonic() + timeout
    while True:
        v = await pred()
        if v:
            return v
        if time.monotonic() > deadline:
            return v
        await asyncio.sleep(step)


class Rig:
    pass


@pytest.fixture
async def rig():
    r = Rig()
    r.layout = load_layout(ROOT / "terminal.yaml")
    r.base = _free_base_port()
    env = salabim.Environment(trace=False, yieldless=False)
    r.server = None
    r.sts = STS(
        equipment_id="STS-01", kind=Kind.STS, position=Position(0.0, 0.0),
        rng=random.Random(1), emit=lambda ev: r.server.on_emit(ev), env=env,
        lane_cells=((4, 0), (5, 0)), grid=Grid(20, 6, 20.0),
    )
    r.server = EquipmentServer(r.sts, port=r.base, host="127.0.0.1")
    r.gw_port = _free_port()
    r.gateway = Gateway(r.layout, port=r.gw_port, upstream_host="127.0.0.1",
                        base_port=r.base, n_sts=1, n_agv=0, host="127.0.0.1")
    r.client = None
    r.control = None

    async def connect_client():
        r.client = Client(r.gateway.endpoint)
        await r.client.connect()
        r.idx = await r.client.get_namespace_index(NS)
        r.obj = await r.client.nodes.objects.get_child(
            [f"{r.idx}:Terminal", f"{r.idx}:Quay", f"{r.idx}:STS-01"])

    r.connect_client = connect_client
    yield r
    if r.client is not None:
        await r.client.disconnect()
    await r.gateway.stop()
    await r.server.stop()
    if r.control is not None:
        await r.control.stop()


async def _val(r, name):
    return await (await r.obj.get_child(f"{r.idx}:{name}")).read_value()


async def _start_connected(r):
    await r.server.start()
    await r.gateway.start()
    await r.connect_client()
    assert await _wait_for(lambda: _is(r, "State", "IDLE"), 3.0)


async def _is(r, name, value):
    return await _val(r, name) == value


async def test_mirror_reflects_variable_change(rig):
    await _start_connected(rig)
    rig.sts.set_state(State.BUSY)
    t0 = time.monotonic()
    await rig.server.sync()
    assert await _wait_for(lambda: _is(rig, "State", "BUSY"), 0.2, step=0.005)
    rig.latency_s = time.monotonic() - t0
    print(f"mirror latency: {rig.latency_s * 1000:.0f} ms")
    # LastEvent / EventSeq are mirrored verbatim (no renumbering)
    assert await _val(rig, "EventSeq") == 1
    ev = event_from_json(await _val(rig, "LastEvent"))
    assert ev.new_state == State.BUSY and ev.equipment_id == "STS-01"


async def test_method_forwarded(rig):
    await _start_connected(rig)
    rig.sts.set_state(State.BUSY)
    await rig.server.sync()
    assert await rig.obj.call_method(f"{rig.idx}:Reset") is True
    assert rig.sts.state == State.IDLE
    # forwarding a call to an absent upstream yields False, not an error
    asc = await rig.client.nodes.objects.get_child(
        [f"{rig.idx}:Terminal", f"{rig.idx}:Yard", f"{rig.idx}:Block-A", f"{rig.idx}:ASC-A"])
    assert await asc.call_method(f"{rig.idx}:Reset") is False


async def test_gateway_before_sim_recovers(rig):
    await rig.gateway.start()
    await rig.connect_client()
    assert await _val(rig, "State") == "FAULT"
    seen = []

    class H:
        async def datachange_notification(self, node, val, data):
            seen.append((name_of[node.nodeid], val))

    seq_node = await rig.obj.get_child(f"{rig.idx}:EventSeq")
    le_node = await rig.obj.get_child(f"{rig.idx}:LastEvent")
    name_of = {seq_node.nodeid: "EventSeq", le_node.nodeid: "LastEvent"}
    sub = await rig.client.create_subscription(10, H())
    await sub.subscribe_data_change([le_node, seq_node], queuesize=50)

    await rig.server.start()
    assert await _wait_for(lambda: _is(rig, "State", "IDLE"), 6.5, step=0.1)
    faults = [event_from_json(v) for n, v in seen if n == "LastEvent" and v]
    assert any(e.kind == EventKind.STATE_CHANGED and e.new_state == State.FAULT for e in faults)
    assert ("EventSeq", 1) in seen
    # upstream restarted at 0: the gateway keeps its sequence monotonic
    assert await _val(rig, "EventSeq") == 1
    rig.sts.set_state(State.BUSY)
    await rig.server.sync()
    assert await _wait_for(lambda: _is(rig, "EventSeq", 2), 1.0)
    assert event_from_json(await _val(rig, "LastEvent")).new_state == State.BUSY
    await sub.delete()


async def test_server_drop_marks_fault(rig):
    await _start_connected(rig)
    seq0 = await _val(rig, "EventSeq")
    await rig.server.stop()
    assert await _wait_for(lambda: _is(rig, "State", "FAULT"), 3.0, step=0.05)
    assert await _val(rig, "EventSeq") == seq0 + 1
    ev = event_from_json(await _val(rig, "LastEvent"))
    assert (ev.kind, ev.new_state, ev.equipment_id) == (EventKind.STATE_CHANGED, State.FAULT, "STS-01")


async def test_prefill_forwarded(rig):
    world = SimpleNamespace(
        now=lambda: 0.0, yard=Yard(rig.layout.blocks),
        scenario=SimpleNamespace(speed_factor=10.0))
    rig.control = ControlServer(world, port=rig.base + 12, on_stop=lambda: None, host="127.0.0.1")
    await rig.control.start()
    await rig.control.sync()
    await _start_connected(rig)
    clock = await rig.client.nodes.objects.get_child([f"{rig.idx}:Terminal", f"{rig.idx}:Clock"])
    speed = await clock.get_child(f"{rig.idx}:SpeedFactor")
    assert await _wait_for(lambda: _eq(speed, 10.0), 6.0, step=0.05)
    payload = '[{"slot":"A/1/1/1","container_id":"P1"}]'
    assert await clock.call_method(f"{rig.idx}:PrefillYard", payload) is True
    assert world.yard.locate("P1") is not None
    assert await clock.call_method(f"{rig.idx}:PrefillYard", payload) is False


async def _eq(node, value):
    return await node.read_value() == value


def test_serve_sigterm_exits_cleanly(tmp_path):
    port = _free_port()
    err = (tmp_path / "stderr.txt").open("w+")
    proc = subprocess.Popen(
        [sys.executable, "-m", "terminal_gateway.serve", "--port", str(port),
         "--upstream-host", "127.0.0.1", "--base-port", str(_free_base_port())],
        cwd=ROOT, stderr=err, stdout=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.2)
        else:
            pytest.fail("gateway did not listen")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    err.seek(0)
    assert "Traceback" not in err.read()
