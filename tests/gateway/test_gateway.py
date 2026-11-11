import asyncio
import logging
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
from event_sink import EventSink
from tcp_proxy import TcpProxy
from terminal_gateway.gateway import Gateway
from terminal_model.port import EquipmentEvent, EventKind, LinkReason
from terminal_model.types import Kind, Position, State
from terminal_opcua.events import resolve_event_types
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
    r.sink = EventSink()

    async def connect_client():
        r.client = Client(r.gateway.endpoint)
        await r.client.connect()
        r.idx = await r.client.get_namespace_index(NS)
        r.obj = await r.client.nodes.objects.get_child(
            [f"{r.idx}:Terminal", f"{r.idx}:Quay", f"{r.idx}:STS-01"])

    r.connect_client = connect_client
    yield r
    if r.client is not None:
        await r.sink.detach()
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
    await r.sink.attach(r.client)
    assert await _wait_for(lambda: _is(r, "State", "IDLE"), 3.0)
    assert await _linked(r, 3.0)


async def _linked(r, timeout: float) -> bool:
    """The STS-01 mirror finished (re)connecting: its subscriptions are in place."""
    return await _wait_until(lambda: r.gateway.mirrors["STS-01"].connected, timeout)


def _sts(r) -> list[tuple[int, EquipmentEvent]]:
    """Events the gateway emitted for STS-01 (the other mirrors have no upstream)."""
    return [(seq, ev) for seq, ev in r.sink.received if ev.equipment_id == "STS-01"]


def _jobs(r) -> list[tuple[int, str | None]]:
    return [(seq, ev.job_id) for seq, ev in _sts(r) if ev.kind == EventKind.JOB_COMPLETED]


async def _wait_until(pred, timeout):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return pred()


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
    assert await _wait_until(lambda: len(_sts(rig)) >= 1, 1.0)
    [(seq, ev)] = _sts(rig)
    assert seq == 1 and ev.new_state == State.BUSY and ev.equipment_id == "STS-01"


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
    await rig.sink.attach(rig.client)
    assert await _val(rig, "State") == "OFFLINE"
    # the OFFLINE for the failed first connect has seq 1 (it may precede the sink)
    assert await _wait_until(lambda: rig.gateway.mirrors["STS-01"]._emitter.seq == 1, 1.0)
    await rig.server.start()
    assert await _wait_for(lambda: _is(rig, "State", "IDLE"), 6.5, step=0.1)
    assert await _wait_until(lambda: any(ev.reason == "UPSTREAM_RECONNECTED" for _, ev in _sts(rig)), 1.0)
    assert await _linked(rig, 2.0)
    got = [(seq, ev.new_state, ev.reason) for seq, ev in _sts(rig)]
    expected = [(1, State.OFFLINE, "UPSTREAM_DISCONNECTED"), (2, State.IDLE, "UPSTREAM_RECONNECTED")]
    assert got in (expected, expected[1:])
    rig.sts.set_state(State.BUSY)
    await rig.server.sync()
    assert await _wait_until(lambda: _sts(rig)[-1][0] == 3, 1.0)
    assert _sts(rig)[-1][1].new_state == State.BUSY


async def test_server_drop_marks_offline(rig):
    await _start_connected(rig)
    await rig.server.stop()
    assert await _wait_for(lambda: _is(rig, "State", "OFFLINE"), 3.0, step=0.05)
    assert await _wait_until(lambda: len(_sts(rig)) >= 1, 1.0)
    [(seq, ev)] = _sts(rig)
    assert seq == 1
    assert (ev.kind, ev.new_state, ev.reason) == (
        EventKind.STATE_CHANGED, State.OFFLINE, LinkReason.UPSTREAM_DISCONNECTED)


async def test_prefill_forwarded(rig):
    world = SimpleNamespace(
        now=lambda: 0.0, yard=Yard(rig.layout.blocks),
        scenario=SimpleNamespace(speed_factor=10.0))
    started = []
    rig.control = ControlServer(world, port=rig.base + 12, on_stop=lambda: None, host="127.0.0.1",
                                on_start=lambda: started.append(1) or True)
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
    assert await clock.call_method(f"{rig.idx}:Start") is True and started == [1]


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

def _done(job_id):
    return EquipmentEvent(EventKind.JOB_COMPLETED, "STS-01", 1.0, job_id=job_id)


async def _emit_jobs(rig, *job_ids):
    for j in job_ids:
        rig.server.on_emit(_done(j))
    await rig.server.sync()


async def test_three_events_in_one_sync_delivered_in_order(rig):
    await _start_connected(rig)
    await _emit_jobs(rig, "J1", "J2", "J3")
    assert await _wait_until(lambda: len(_jobs(rig)) >= 3, 1.0)
    assert _jobs(rig) == [(1, "J1"), (2, "J2"), (3, "J3")]


async def _blip(rig, during=()):
    """Cut the link, emit ``during`` upstream while cut, restore and wait for the recovery."""
    rig.proxy.cut()
    assert await _wait_until(
        lambda: any(ev.reason == "UPSTREAM_DISCONNECTED" for _, ev in _sts(rig)), 3.0)
    if during:
        await _emit_jobs(rig, *during)
    rig.proxy.restore()
    assert await _wait_until(
        lambda: any(ev.reason == "UPSTREAM_RECONNECTED" for _, ev in _sts(rig)), 4.0)
    assert await _linked(rig, 2.0)


@pytest.mark.parametrize("rig", [1.0], indirect=True)
async def test_blip_with_one_event_logs_gap(rig, caplog):
    caplog.set_level(logging.WARNING, logger="terminal_gateway.mirror")
    await _start_connected(rig)
    await _emit_jobs(rig, "J5")  # upstream Seq 1
    assert await _wait_until(lambda: len(_jobs(rig)) == 1, 1.0)
    await _blip(rig, during=["J6"])  # upstream Seq 2: lost
    await _emit_jobs(rig, "J7")  # upstream Seq 3
    assert await _wait_until(lambda: len(_jobs(rig)) == 2, 1.0)
    got = [(seq, ev.job_id or ev.reason) for seq, ev in _sts(rig)]
    assert got == [(1, "J5"), (2, "UPSTREAM_DISCONNECTED"), (3, "UPSTREAM_RECONNECTED"), (4, "J7")]
    gaps = [r.getMessage() for r in caplog.records if "Seq gap" in r.getMessage()]
    assert gaps == ["STS-01: upstream Seq gap 1 -> 3"]


@pytest.mark.parametrize("rig", [1.0], indirect=True)
async def test_blip_without_events_only_offline_and_recovery(rig, caplog):
    caplog.set_level(logging.WARNING, logger="terminal_gateway.mirror")
    await _start_connected(rig)
    await _blip(rig)
    await asyncio.sleep(0.2)  # nothing else may follow
    [(_, fault), (rec_seq, rec)] = _sts(rig)
    assert fault.new_state == State.OFFLINE and rec.new_state == State.IDLE
    await _emit_jobs(rig, "J1", "J2")
    assert await _wait_until(lambda: len(_jobs(rig)) >= 2, 1.0)
    assert _jobs(rig) == [(rec_seq + 1, "J1"), (rec_seq + 2, "J2")]
    assert not [r for r in caplog.records if "Seq gap" in r.getMessage()]


@pytest.mark.parametrize("rig", [1.0], indirect=True)
async def test_blip_on_idle_upstream_reports_recovery_to_idle(rig):
    """The upstream never faulted: without the recovery the ECS would exclude it for good."""
    await _start_connected(rig)
    rig.gateway.mirrors["STS-01"].set_sim_time(42.0)
    await _blip(rig)
    (fault_seq, fault), (rec_seq, rec) = _sts(rig)
    assert (fault.kind, fault.new_state, fault.reason) == (
        EventKind.STATE_CHANGED, State.OFFLINE, LinkReason.UPSTREAM_DISCONNECTED)
    assert (rec.kind, rec.new_state, rec.reason) == (
        EventKind.STATE_CHANGED, State.IDLE, "UPSTREAM_RECONNECTED")
    assert rec.equipment_id == "STS-01" and rec.sim_time == 42.0
    assert rec_seq == fault_seq + 1
    assert await _val(rig, "State") == "IDLE"


@pytest.mark.parametrize("rig", [1.0], indirect=True)
async def test_blip_on_busy_upstream_reports_busy(rig):
    await _start_connected(rig)
    rig.sts.set_state(State.BUSY)
    await rig.server.sync()
    assert await _wait_for(lambda: _is(rig, "State", "BUSY"), 1.0)
    await _blip(rig)
    rec = _sts(rig)[-1][1]
    assert (rec.new_state, rec.reason) == (State.BUSY, "UPSTREAM_RECONNECTED")
    assert await _val(rig, "State") == "BUSY"


@pytest.mark.parametrize("rig", [1.0], indirect=True)
async def test_blip_on_faulted_upstream_reports_fault(rig):
    await _start_connected(rig)
    rig.sts.set_state(State.FAULT)
    await rig.server.sync()
    assert await _wait_until(lambda: len(_sts(rig)) == 1, 1.0)  # upstream's own Fault
    await _blip(rig)
    got = [(ev.new_state, ev.reason) for _, ev in _sts(rig)][1:]
    assert got == [(State.OFFLINE, "UPSTREAM_DISCONNECTED"), (State.FAULT, "UPSTREAM_RECONNECTED")]
    assert await _val(rig, "State") == "FAULT"


@pytest.mark.parametrize("rig", [1.0], indirect=True)
async def test_blip_with_unknown_upstream_state_sends_no_recovery(rig, caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="terminal_gateway.mirror")
    await _start_connected(rig)
    mirror = rig.gateway.mirrors["STS-01"]
    emitted = []
    real_emit = mirror._emit

    async def spy(ev):
        emitted.append(ev)
        await real_emit(ev)

    monkeypatch.setattr(mirror, "_emit", spy)
    mirror._fault_announced = True
    async with mirror._write_lock:
        await mirror._announce_recovery("BOGUS")
    assert emitted == []
    assert any("unknown" in r.getMessage() for r in caplog.records)


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
async def test_fresh_upstream_restart_keeps_gateway_seq_monotonic(rig, caplog):
    caplog.set_level(logging.WARNING, logger="terminal_gateway.mirror")
    await _start_connected(rig)
    await _emit_jobs(rig, "J1")
    assert await _wait_until(lambda: len(_jobs(rig)) == 1, 1.0)
    await rig.server.stop()
    assert await _wait_until(
        lambda: any(ev.reason == "UPSTREAM_DISCONNECTED" for _, ev in _sts(rig)), 3.0)
    await rig.server.start()  # fresh server on the same port: upstream Seq restarts at 1
    assert await _wait_until(
        lambda: any(ev.reason == "UPSTREAM_RECONNECTED" for _, ev in _sts(rig)), 4.0)
    assert await _linked(rig, 2.0)
    await _emit_jobs(rig, "J2", "J3")
    assert await _wait_until(lambda: len(_jobs(rig)) == 3, 1.0)
    got = [(seq, ev.job_id or ev.reason) for seq, ev in _sts(rig)]
    assert got == [(1, "J1"), (2, "UPSTREAM_DISCONNECTED"), (3, "UPSTREAM_RECONNECTED"),
                   (4, "J2"), (5, "J3")]
    assert not [r for r in caplog.records if "not ahead" in r.getMessage() or "Seq gap" in r.getMessage()]


async def test_event_for_other_equipment_ignored(rig, caplog):
    caplog.set_level(logging.WARNING, logger="terminal_gateway.mirror")
    await _start_connected(rig)
    async with Client(rig.server.endpoint) as up:
        up_types = await resolve_event_types(up)
    mirror = rig.gateway.mirrors["STS-01"]
    await mirror.event_notification(SimpleNamespace(
        EventType=up_types.job_completed, EquipmentId="AGV-99", Seq=1, SimTime=0.0,
        JobId="J", Reason="", NewState=""))
    await asyncio.sleep(0.2)
    assert mirror._emitter.seq == 0
    assert not [ev for _, ev in rig.sink.received if ev.equipment_id in ("AGV-99", "STS-01")]
    # ignored (spec section 7), not skipped as undecodable
    assert not [r for r in caplog.records if "undecodable" in r.getMessage()]


@pytest.mark.parametrize("rig", [1.0], indirect=True)
async def test_reconnect_window_state_change_still_delivered(rig, monkeypatch):
    """Upstream BUSY -> IDLE right around the snapshot read after a blip: the IDLE event
    must still reach the ECS after the recovery, or it would exclude the device for good."""
    await _start_connected(rig)
    rig.sts.set_state(State.BUSY)
    await rig.server.sync()
    assert await _wait_for(lambda: _is(rig, "State", "BUSY"), 1.0)
    real_read_values = Client.read_values
    armed = []

    async def read_then_finish(self, nodes):
        values = await real_read_values(self, nodes)
        if armed and armed.pop():  # once, on the mirror's reconnect snapshot read
            rig.sts.set_state(State.IDLE)  # emits STATE_CHANGED -> IDLE upstream
            await rig.server.sync()
            await asyncio.sleep(0.3)  # let the event reach the gateway client meanwhile
        return values

    monkeypatch.setattr(Client, "read_values", read_then_finish)
    rig.proxy.cut()
    assert await _wait_until(
        lambda: any(ev.reason == "UPSTREAM_DISCONNECTED" for _, ev in _sts(rig)), 3.0)
    armed.append(True)
    rig.proxy.restore()
    assert await _wait_until(
        lambda: any(ev.reason == "UPSTREAM_RECONNECTED" for _, ev in _sts(rig)), 4.0)
    assert not armed  # the window was actually hit
    [(rec_seq, rec)] = [(seq, ev) for seq, ev in _sts(rig) if ev.reason == "UPSTREAM_RECONNECTED"]
    assert rec.new_state == State.BUSY  # the snapshot read saw BUSY

    def idle_after_recovery():
        return any(seq > rec_seq and ev.kind == EventKind.STATE_CHANGED
                   and ev.new_state == State.IDLE for seq, ev in _sts(rig))

    assert await _wait_until(idle_after_recovery, 2.0)
    assert _sts(rig)[-1][1].new_state == State.IDLE
    assert await _wait_for(lambda: _is(rig, "State", "IDLE"), 1.0)
