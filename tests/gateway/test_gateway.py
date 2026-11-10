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
from tcp_proxy import TcpProxy
from terminal_gateway.gateway import Gateway
from terminal_model.port import EquipmentEvent, EventKind
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
async def rig(request):
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
    proxied = getattr(request, "param", None) is not None
    # proxied: gateway -> proxy on base_port -> upstream on its own port
    r.server = EquipmentServer(r.sts, port=_free_port() if proxied else r.base, host="127.0.0.1")
    r.proxy = TcpProxy(r.base, r.server.port) if proxied else None
    if r.proxy is not None:
        await r.proxy.start()
    r.gw_port = _free_port()
    r.gateway = Gateway(r.layout, port=r.gw_port, upstream_host="127.0.0.1",
                        base_port=r.base, n_sts=1, n_agv=0, host="127.0.0.1",
                        reconnect_s=getattr(request, "param", 5.0))
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
    if r.proxy is not None:
        await r.proxy.stop()


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


# -- end-to-end event delivery as the ECS port will consume it ------------------------

class EventConsumer:
    """Subscribes LastEvent+EventSeq (queue 100) and pairs them FIFO, like the ECS port."""

    def __init__(self):
        self.queue: list[str] = []
        self.delivered: list[tuple[int, EquipmentEvent]] = []
        self.le_writes = 0

    async def attach(self, r):
        self.le = await r.obj.get_child(f"{r.idx}:LastEvent")
        self.seq = await r.obj.get_child(f"{r.idx}:EventSeq")
        self.sub = await r.client.create_subscription(10, self)
        await self.sub.subscribe_data_change([self.le, self.seq], queuesize=100)
        await asyncio.sleep(0.3)
        self.delivered.clear()
        self.le_writes = 0

    async def datachange_notification(self, node, val, data):
        if node.nodeid == self.le.nodeid:
            self.queue.append(val)
            self.le_writes += 1
        else:
            payload = self.queue.pop(0) if self.queue else ""
            self.delivered.append((val, event_from_json(payload) if payload else None))

    def jobs(self):
        return [(seq, ev.job_id) for seq, ev in self.delivered]


def _done(job_id):
    return EquipmentEvent(EventKind.JOB_COMPLETED, "STS-01", 1.0, job_id=job_id)


async def _wait_until(pred, timeout):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return pred()


async def test_three_events_in_one_sync_delivered_in_order(rig):
    await _start_connected(rig)
    c = EventConsumer()
    await c.attach(rig)
    n = await _val(rig, "EventSeq")
    for j in ("J1", "J2", "J3"):
        rig.server.on_emit(_done(j))
    await rig.server.sync()
    assert await _wait_until(lambda: len(c.delivered) >= 3, 1.0)
    await asyncio.sleep(0.2)
    assert c.jobs() == [(n + 1, "J1"), (n + 2, "J2"), (n + 3, "J3")]


async def _blip(rig, c, during=()):
    """Cut the link, emit ``during`` upstream while cut, restore and wait for resync."""
    rig.proxy.cut()
    assert await _wait_until(
        lambda: any(ev and ev.new_state == State.FAULT for _, ev in c.delivered), 3.0)
    for j in during:
        rig.server.on_emit(_done(j))
    await rig.server.sync()
    rig.proxy.restore()
    assert await _wait_for(lambda: _is(rig, "State", "IDLE"), 4.0, step=0.05)
    await asyncio.sleep(0.3)


@pytest.mark.parametrize("rig", [1.0], indirect=True)
async def test_blip_with_one_event_delivers_it(rig):
    await _start_connected(rig)
    c = EventConsumer()
    await c.attach(rig)
    await _blip(rig, c, during=["J6"])
    (fault_seq, fault), (j6_seq, j6) = c.delivered
    assert fault.new_state == State.FAULT and fault.reason == "UPSTREAM_DISCONNECTED"
    assert j6.job_id == "J6" and j6_seq > fault_seq
    for j in ("J7", "J8"):
        rig.server.on_emit(_done(j))
    await rig.server.sync()
    assert await _wait_until(lambda: len(c.delivered) >= 4, 1.0)
    assert c.jobs()[2:] == [(j6_seq + 1, "J7"), (j6_seq + 2, "J8")]


@pytest.mark.parametrize("rig", [1.0], indirect=True)
async def test_blip_without_events_writes_no_last_event(rig):
    await _start_connected(rig)
    c = EventConsumer()
    await c.attach(rig)
    await _blip(rig, c)
    assert c.le_writes == 1  # only the synthesized Fault
    [(fault_seq, fault)] = c.delivered
    assert fault.new_state == State.FAULT
    for j in ("J1", "J2"):
        rig.server.on_emit(_done(j))
    await rig.server.sync()
    assert await _wait_until(lambda: len(c.delivered) >= 3, 1.0)
    await asyncio.sleep(0.2)
    assert c.jobs()[1:] == [(fault_seq + 1, "J1"), (fault_seq + 2, "J2")]
    assert c.queue == []


async def test_start_failure_stops_server(rig, monkeypatch):
    from terminal_gateway import gateway as gw_mod

    real_start = gw_mod.EquipmentMirror.start
    calls = []

    async def boom(self):
        calls.append(self.equipment_id)
        if len(calls) == 2:  # first mirror created, second fails
            raise RuntimeError("boom")
        await real_start(self)

    monkeypatch.setattr(gw_mod.EquipmentMirror, "start", boom)
    with pytest.raises(RuntimeError):
        await rig.gateway.start()
    assert rig.gateway.mirrors == {} and rig.gateway.control is None
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", rig.gw_port)) != 0


@pytest.mark.parametrize("rig", [1.0], indirect=True)
async def test_fresh_upstream_restart_emits_no_empty_event(rig):
    await _start_connected(rig)
    c = EventConsumer()
    await c.attach(rig)
    rig.server.on_emit(_done("J1"))
    await rig.server.sync()
    assert await _wait_until(lambda: len(c.delivered) >= 1, 1.0)
    await rig.server.stop()
    assert await _wait_until(
        lambda: any(ev and ev.new_state == State.FAULT for _, ev in c.delivered), 3.0)
    await rig.server.start()  # fresh server on the same port: EventSeq 0, LastEvent ""
    assert await _val_eventually(rig, "State", "IDLE", 4.0)
    await asyncio.sleep(0.3)
    assert c.le_writes == 2  # J1 and the synthesized Fault, no "" payload
    (j1_seq, j1), (fault_seq, fault) = c.delivered
    assert j1.job_id == "J1" and fault.new_state == State.FAULT
    for j in ("J2", "J3"):
        rig.server.on_emit(_done(j))
    await rig.server.sync()
    assert await _wait_until(lambda: len(c.delivered) >= 4, 1.0)
    await asyncio.sleep(0.2)
    assert c.jobs()[2:] == [(fault_seq + 1, "J2"), (fault_seq + 2, "J3")]
    assert all(ev is not None for _, ev in c.delivered) and c.queue == []


async def _val_eventually(r, name, value, timeout):
    return await _wait_for(lambda: _is(r, name, value), timeout, step=0.05)
