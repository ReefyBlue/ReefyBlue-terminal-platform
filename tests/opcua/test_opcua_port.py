"""OpcUaEquipmentPort against a live gateway in front of per-equipment sim servers.

Servers, gateway and the sim live on this module's event loop; the port runs its
own loop in a thread. Blocking port calls from a test therefore go through
``asyncio.to_thread`` (calling them on the test loop would starve the gateway).
"""

import asyncio
import logging
import socket
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from asyncua import ua
from terminal_gateway.gateway import Gateway
from terminal_model.port import EquipmentEvent, EquipmentPort, EventKind
from terminal_model.types import Job, JobType, State
from terminal_opcua import port as port_mod
from terminal_opcua.port import OpcUaEquipmentPort
from terminal_sim.layout import load_layout
from terminal_sim.opcua_servers import ControlServer, EquipmentServer
from terminal_sim.scenario import load_scenario
from terminal_sim.world import World

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _free_base_port(n_slots: int = 13) -> int:
    """A base port whose ``n_slots`` successors are bindable right now (best effort)."""
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            base = s.getsockname()[1] % 20000 + 20000
        socks = []
        try:
            for p in range(base, base + n_slots):
                sock = socket.socket()
                socks.append(sock)
                sock.bind(("127.0.0.1", p))
            return base
        except OSError:
            continue
        finally:
            for sock in socks:
                sock.close()
    raise RuntimeError("no free port range")


async def _until(pred, timeout: float, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(step)
    return True


class Rig:
    def control_connected(self) -> bool:
        return self.gateway.control is not None and self.gateway.control.connected

    async def step(self, until: float) -> None:
        """What SimService does per tick: advance the world, then sync all servers."""
        self.world.run(until=until)
        await asyncio.gather(*(s.sync() for s in self.servers.values()), self.control.sync())


async def _start_rig() -> Rig:
    r = Rig()
    layout = load_layout(ROOT / "terminal.yaml")
    scenario = replace(load_scenario(ROOT / "scenarios/baseline.yaml"), n_sts=1, n_agv=2)
    r.world = World(layout, scenario)
    base = _free_base_port()
    r.servers = {
        eid: EquipmentServer(eq, port=base + i, host="127.0.0.1")
        for i, (eid, eq) in enumerate(r.world.equipment.items())
    }
    r.sim_events = []  # ground truth: what the sim emitted
    r.world.port.subscribe(r.sim_events.append)
    r.world.port.subscribe(lambda ev: r.servers[ev.equipment_id].on_emit(ev))
    r.started = []
    r.control = ControlServer(
        r.world, port=base + len(r.world.equipment), on_stop=lambda: None, host="127.0.0.1",
        on_start=lambda: r.started.append(1) or True)
    await asyncio.gather(*(s.start() for s in r.servers.values()), r.control.start())
    await r.step(0.0)
    r.gateway = Gateway(layout, port=_free_port(), upstream_host="127.0.0.1",
                        base_port=base, n_sts=1, n_agv=2, host="127.0.0.1")
    await r.gateway.start()
    assert await _until(
        lambda: r.control_connected() and all(m.connected for m in r.gateway.mirrors.values()),
        10.0, step=0.05)
    # an event that already happened before the port connects must not be delivered
    r.servers["STS-01"].on_emit(
        EquipmentEvent(EventKind.STATE_CHANGED, "STS-01", 0.0, new_state=State.IDLE))
    await r.step(0.0)
    assert await _until(lambda: r.gateway.mirrors["STS-01"]._emitter.seq == 1, 2.0)
    r.port = OpcUaEquipmentPort(r.gateway.endpoint, connect_timeout_s=5.0)
    r.events = []
    r.port.subscribe(r.events.append)
    await asyncio.to_thread(r.port.connect)
    return r


async def _stop_rig(r: Rig) -> None:
    await asyncio.to_thread(r.port.close)
    await r.gateway.stop()
    await asyncio.gather(*(s.stop() for s in [*r.servers.values(), r.control]))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def rig():
    r = await _start_rig()
    yield r
    await _stop_rig(r)


async def _drain_until(r, pred, timeout: float) -> bool:
    """Drain into ``r.events`` until ``pred(r.events)``; polls every 5 ms."""
    return await _until(lambda: (r.port.drain_events(), pred(r.events))[1], timeout, step=0.005)


async def test_port_satisfies_protocol(rig):
    assert isinstance(rig.port, EquipmentPort)


async def test_initial_values_not_delivered(rig):
    await asyncio.sleep(0.3)
    assert rig.port.drain_events() == 0


async def test_equipment_ids_order(rig):
    assert rig.port.equipment_ids() == [
        "STS-01", "AGV-01", "AGV-02", "ASC-A", "ASC-B", "ASC-C", "ASC-D"]


async def test_snapshot_matches_sim(rig):
    for eid, eq in rig.world.equipment.items():
        want = eq.snapshot()

        async def matches(eid=eid, want=want):
            got = await asyncio.to_thread(rig.port.snapshot, eid)
            return got if (got.position, got.state) == (want.position, want.state) else None

        deadline = time.monotonic() + 2.0
        while (got := await matches()) is None and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert got is not None, eid
        assert (got.equipment_id, got.kind, got.vendor) == (eid, want.kind, want.vendor)
        assert got.current_job_id == want.current_job_id
        assert got.energy_kwh == pytest.approx(want.energy_kwh)
        assert dict(got.extra) == dict(want.extra)
    agv = await asyncio.to_thread(rig.port.snapshot, "AGV-01")
    assert agv.extra["battery_pct"] == rig.world.equipment["AGV-01"].snapshot().extra["battery_pct"]


async def test_assign_and_drain_events(rig):
    rig.port.drain_events()
    rig.events.clear()
    rig.sim_events.clear()
    job = Job("J1", JobType.TRANSPORT, "C1", "STS-01", "TP/A/WS")
    t0 = time.monotonic()
    assert await asyncio.to_thread(rig.port.assign_job, "AGV-01", job) is True
    assign_ms = (time.monotonic() - t0) * 1000
    await rig.step(rig.world.now() + 200)
    t1 = time.monotonic()
    agv = lambda evs: [e for e in evs if e.equipment_id == "AGV-01"]
    assert await _drain_until(
        rig, lambda evs: any(e.kind == EventKind.JOB_COMPLETED for e in agv(evs)), 2.0)
    event_ms = (time.monotonic() - t1) * 1000
    print(f"assign_job round trip: {assign_ms:.0f} ms; event delivery: {event_ms:.0f} ms")
    await asyncio.sleep(0.2)
    rig.port.drain_events()
    mine = agv(rig.events)
    assert len(mine) >= 2
    # the sim emits BUSY, JOB_COMPLETED, IDLE: the job's completion is followed by
    # the IDLE state change at the same sim time, so compare against the sim itself
    assert mine == agv(rig.sim_events)
    assert [e.kind for e in mine][-2:] == [EventKind.JOB_COMPLETED, EventKind.STATE_CHANGED]
    assert mine[-2].job_id == "J1" and mine[-1].new_state == State.IDLE


async def test_now_follows_sim_time(rig):
    t = rig.world.now() + 10
    await rig.step(t)
    assert await asyncio.to_thread(rig.port.wait_for_sim_time, t, 2.0) is True
    assert rig.port.now() == t
    assert await asyncio.to_thread(rig.port.wait_for_sim_time, t + 1000, 0.1) is False


async def test_start_sim_forwarded(rig):
    rig.started.clear()
    assert await asyncio.to_thread(rig.port.start_sim) is True
    assert rig.started == [1]


async def test_unknown_id_keyerror(rig):
    job = Job("J9", JobType.TRANSPORT, "C9", "STS-01", "TP/A/WS")
    for call in (
        lambda: rig.port.snapshot("NOPE"),
        lambda: rig.port.assign_job("NOPE", job),
        lambda: rig.port.cancel_job("NOPE"),
        lambda: rig.port.reset("NOPE"),
    ):
        with pytest.raises(KeyError):
            call()


async def test_events_seq_gap_logged(rig, caplog):
    rig.port.drain_events()
    rig.events.clear()
    caplog.set_level(logging.WARNING, logger="terminal_opcua.port")
    mirror = rig.gateway.mirrors["ASC-D"]
    first = EquipmentEvent(EventKind.STATE_CHANGED, "ASC-D", rig.world.now(), new_state=State.BUSY)
    rig.servers["ASC-D"].on_emit(first)
    await rig.servers["ASC-D"].sync()
    assert await _drain_until(rig, lambda evs: any(e.equipment_id == "ASC-D" for e in evs), 2.0)
    mirror._emitter.seq += 1  # the gateway skips one Seq: the port sees a gap
    ev = EquipmentEvent(EventKind.STATE_CHANGED, "ASC-D", rig.world.now(), new_state=State.IDLE)
    rig.servers["ASC-D"].on_emit(ev)
    await rig.servers["ASC-D"].sync()
    assert await _drain_until(rig, lambda evs: ev in evs, 2.0)
    assert [e for e in rig.events if e.equipment_id == "ASC-D"] == [first, ev]
    assert any("Seq gap" in r.getMessage() and "ASC-D" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


async def test_two_events_one_sync_in_order(rig):
    rig.port.drain_events()
    rig.events.clear()
    t = rig.world.now()
    done = EquipmentEvent(EventKind.JOB_COMPLETED, "ASC-C", t, job_id="J9")
    idle = EquipmentEvent(EventKind.STATE_CHANGED, "ASC-C", t, new_state=State.IDLE)
    server = rig.servers["ASC-C"]
    server.on_emit(done)
    server.on_emit(idle)
    await server.sync()
    mine = lambda evs: [e for e in evs if e.equipment_id == "ASC-C"]
    assert await _drain_until(rig, lambda evs: len(mine(evs)) >= 2, 2.0)
    await asyncio.sleep(0.1)
    rig.port.drain_events()
    assert mine(rig.events) == [done, idle]


async def test_unknown_event_type_ignored(rig, caplog):
    rig.port.drain_events()
    caplog.set_level(logging.WARNING, logger="terminal_opcua.port")
    rig.port._on_event(SimpleNamespace(EventType=ua.NodeId(ua.ObjectIds.SystemEventType)))
    assert rig.port.drain_events() == 0
    assert [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "terminal_opcua.port"] == [
        f"undecodable event skipped: unknown event type: {ua.NodeId(ua.ObjectIds.SystemEventType)}"]


async def test_connect_failure_raises_connection_error():
    port = OpcUaEquipmentPort(f"opc.tcp://127.0.0.1:{_free_port()}/none", connect_timeout_s=0.5)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        await asyncio.to_thread(port.connect)
    assert time.monotonic() - t0 < 5.0
    port.close()  # idempotent after a failed connect
    port.close()


async def test_connection_lost_fails_fast():
    r = await _start_rig()
    try:
        await r.gateway.stop()
        lost = await _until(lambda: r.port._lost, 10.0, step=0.05)
        assert lost, "connection loss not detected"
        # "Fast" = well inside the call timeout, not after waiting it out. Measured around the
        # failing call only (not the thread hop), with a 2 s floor against full-suite load.
        bound = max(2.0, 0.5 * port_mod.CALL_TIMEOUT_S)
        assert bound < port_mod.CALL_TIMEOUT_S

        def timed(fn, *args):
            t0 = time.monotonic()
            with pytest.raises(ConnectionError):
                fn(*args)
            return time.monotonic() - t0

        assert await asyncio.to_thread(timed, r.port.snapshot, "STS-01") < bound
        sim_t = r.port.now() + 1000
        assert await asyncio.to_thread(timed, r.port.wait_for_sim_time, sim_t, 30.0) < bound
    finally:
        await asyncio.to_thread(r.port.close)
        await asyncio.gather(*(s.stop() for s in [*r.servers.values(), r.control]))


async def test_drain_events_raises_after_connection_lost():
    port = OpcUaEquipmentPort("opc.tcp://127.0.0.1:1/none")
    got = []
    port.subscribe(got.append)
    ev = EquipmentEvent(EventKind.STATE_CHANGED, "STS-01", 0.0, new_state=State.IDLE)
    port._events.put(ev)
    assert port.drain_events() == 1  # not lost: delivers and returns
    port._events.put(ev)
    port._lost = True
    with pytest.raises(ConnectionError):
        port.drain_events()
    assert got == [ev, ev]  # buffered events delivered before the error


async def test_close_discards_buffered_events():
    port = OpcUaEquipmentPort("opc.tcp://127.0.0.1:1/none")
    got = []
    port.subscribe(got.append)
    port._events.put(EquipmentEvent(EventKind.STATE_CHANGED, "STS-01", 0.0, new_state=State.IDLE))
    port.close()
    assert port.drain_events() == 0 and got == []
