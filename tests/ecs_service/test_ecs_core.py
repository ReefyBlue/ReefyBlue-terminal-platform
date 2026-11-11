from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fake_port import FakePort
from terminal_ecs.workorders import WorkOrder
from terminal_ecs_service import core as core_mod
from terminal_ecs_service.core import (
    CONNECT_RETRY_S,
    DRAIN_POLL_S,
    SAMPLE_INTERVAL_S,
    EcsCore,
)
from terminal_ecs_service.events import Broadcaster
from terminal_model.types import JobType, Kind, Position, State

LAYOUT = SimpleNamespace(blocks=[SimpleNamespace(block="A", bays=2, rows=2, tiers=2)])


def _fake(cls=FakePort) -> FakePort:
    return cls(
        {
            "STS-01": (Kind.STS, Position(100, 0)),
            "AGV-01": (Kind.AGV, Position(10, 50)),
            "ASC-A": (Kind.ASC, Position(50, 120)),
        }
    )


def _drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _core(fake):
    b = Broadcaster()
    q = b.register()
    return EcsCore(LAYOUT, connect=lambda: fake, broadcaster=b), q


def test_constants():
    assert (SAMPLE_INTERVAL_S, DRAIN_POLL_S, CONNECT_RETRY_S) == (60.0, 0.005, 2.0)


async def test_connect_once_creates_dispatcher_and_publishes_link():
    fake = _fake()
    fake.advance(10.0)
    core, q = _core(fake)
    assert core.dispatcher is None and core.kpi is None
    assert await core.connect_once() is True
    assert core.connected is True and core.dispatcher is not None and core.kpi is not None
    assert core.t0 == 10.0 and core.next_tick == 70.0
    assert _drain(q) == [{"type": "link", "connected": True, "sim_time": 10.0}]


async def test_step_ticks_on_60s_boundaries():
    fake = _fake()
    core, q = _core(fake)
    await core.connect_once()
    _drain(q)
    fake.advance(59)
    core.step()
    assert core.tick_count == 0
    fake.advance(1)
    core.step()
    assert core.tick_count == 1
    msgs = _drain(q)
    assert [m["type"] for m in msgs] == ["status"]
    assert msgs[0]["sim_time"] == 60.0 and msgs[0]["open"] == 0 and "equipment" in msgs[0]
    fake.advance(120)
    core.step()
    assert core.tick_count == 3


async def test_equipment_events_are_broadcast():
    fake = _fake()
    core, q = _core(fake)
    await core.connect_once()
    _drain(q)
    fake.advance(5)
    fake.set_state("ASC-A", State.FAULT)
    assert _drain(q) == [
        {
            "type": "equipment",
            "kind": "STATE_CHANGED",
            "equipment_id": "ASC-A",
            "sim_time": 5.0,
            "job_id": None,
            "reason": None,
            "new_state": "FAULT",
        }
    ]


async def test_connect_once_false_on_connection_error():
    def refuse():
        raise ConnectionError("gateway down")

    core = EcsCore(LAYOUT, connect=refuse)
    assert await core.connect_once() is False
    assert core.connected is False and core.dispatcher is None


class LosingPort(FakePort):
    lost = False
    closed = 0

    def drain_events(self) -> int:
        if self.lost:
            raise ConnectionError("link lost")
        return 0

    def close(self) -> None:
        self.closed += 1


async def test_step_on_connection_error_unbinds_and_publishes_link(caplog):
    fake = _fake(LosingPort)
    core, q = _core(fake)
    await core.connect_once()
    fake.advance(30)
    core.step()
    _drain(q)
    fake.lost = True
    with caplog.at_level(logging.WARNING):
        core.step()
    assert core.connected is False
    for _ in range(100):  # closed off-loop
        if fake.closed:
            break
        await asyncio.sleep(0.01)
    assert fake.closed == 1
    msgs = _drain(q)
    assert [m for m in msgs if m["type"] == "link"] == [{"type": "link", "connected": False, "sim_time": 30.0}]
    lost = [m for m in msgs if m["type"] == "equipment"]
    assert {m["equipment_id"] for m in lost} == {"AGV-01", "ASC-A", "STS-01"}
    assert all(m["reason"] == "LINK_LOST" and m["new_state"] == "OFFLINE" for m in lost)
    core.step()  # unbound: no-op
    assert _drain(q) == []


async def test_reconnect_keeps_dispatcher_and_t0():
    first = _fake(LosingPort)
    second = _fake()
    ports = [first, second]
    b = Broadcaster()
    core = EcsCore(LAYOUT, connect=lambda: ports.pop(0), broadcaster=b)
    await core.connect_once()
    dispatcher, kpi = core.dispatcher, core.kpi
    first.lost = True
    core.step()
    second.advance(150.0)
    assert await core.connect_once() is True
    assert core.dispatcher is dispatcher and core.kpi is kpi and core.t0 == 0.0
    assert core.next_tick == 180.0  # next boundary after now, no catch-up burst
    second.set_state("AGV-01", State.FAULT)
    assert dispatcher.status()["equipment"]["AGV-01"] is State.FAULT


async def test_submit_occupies_slots_and_publishes_jobs():
    fake = _fake()
    core, q = _core(fake)
    await core.connect_once()
    _drain(q)
    core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])
    assert core.allocator.next_slot("A") != "A/1/1/1"
    jobs = [m for m in _drain(q) if m["type"] == "job"]
    assert len(jobs) == 1
    (job_id,) = core.dispatcher.job_log
    assert jobs[0] == {"type": "job", "job_id": job_id, "equipment_id": "AGV-01", "sim_time": 0.0}
    core.publish_new_jobs()  # already seen
    assert _drain(q) == []


async def test_submit_without_connection_raises():
    core = EcsCore(LAYOUT, connect=lambda: _fake())
    with pytest.raises(RuntimeError):
        core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])


async def test_status_and_kpis_without_connection():
    core = EcsCore(LAYOUT, connect=lambda: _fake())
    st = core.status()
    assert st["connected"] is False and st["open"] == 0 and st["tick_count"] == 0
    assert st["sim_time"] == 0.0 and st["uptime_s"] >= 0.0 and st["equipment"] == {}
    assert core.kpis() == {}
    assert core.equipment() == []


async def test_status_kpis_equipment_when_connected():
    fake = _fake()
    core, _ = _core(fake)
    await core.connect_once()
    fake.advance(60)
    core.step()
    st = core.status()
    assert st["connected"] is True and st["tick_count"] == 1 and st["sim_time"] == 60.0
    k = core.kpis()
    assert k["sim_elapsed_s"] == 60.0 and k["containers_done"] == 0.0
    eq = {e["equipment_id"]: e for e in core.equipment()}
    assert eq["AGV-01"] == {
        "equipment_id": "AGV-01",
        "kind": "AGV",
        "vendor": "FAKE",
        "state": "IDLE",
        "position": {"x": 10, "y": 50},
        "current_job_id": None,
        "energy_kwh": 0.0,
        "extra": {"last_job_id": None, "last_job_outcome": None},
    }


async def test_run_retries_connect_then_steps(monkeypatch):
    fake = _fake()
    attempts = []

    def connect():
        attempts.append(1)
        if len(attempts) < 2:
            raise ConnectionError("not yet")
        return fake

    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:  # connected: let sim time reach the first boundary
            fake.advance(60)
        if len(sleeps) > 5:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(core_mod.asyncio, "sleep", fake_sleep)
    core = EcsCore(LAYOUT, connect=connect)
    with pytest.raises(asyncio.CancelledError):
        await core.run()
    assert len(attempts) == 2
    assert sleeps[0] == CONNECT_RETRY_S and set(sleeps[1:]) == {DRAIN_POLL_S}
    assert core.tick_count == 1


async def test_submit_while_link_lost_raises_not_connected():
    fake = _fake(LosingPort)
    core, _ = _core(fake)
    await core.connect_once()
    fake.lost = True
    core.step()
    assert core.dispatcher is not None and core.connected is False
    with pytest.raises(RuntimeError, match="not connected"):
        core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])


async def test_submit_records_known_containers():
    core, _ = _core(_fake())
    await core.connect_once()
    assert not core.is_known("C1")
    core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1"),
                 WorkOrder("C2", JobType.LOAD, "STS-01", "A/1/1/2")])
    assert core.is_known("C1") and core.is_known("C2") and not core.is_known("C3")


class SlowClosePort(FakePort):
    closed = False

    def close(self) -> None:
        import time as _t

        _t.sleep(0.3)
        self.closed = True


async def test_on_link_lost_closes_port_off_loop():
    import time as _t

    fake = _fake(SlowClosePort)
    core, q = _core(fake)
    await core.connect_once()
    _drain(q)
    t0 = _t.monotonic()
    core.on_link_lost()
    assert _t.monotonic() - t0 < 0.1  # close does not block the loop
    assert core.connected is False and fake.closed is False
    assert [m for m in _drain(q) if m["type"] == "link"] == [{"type": "link", "connected": False, "sim_time": 0.0}]
    for _ in range(100):
        if fake.closed:
            break
        await asyncio.sleep(0.01)
    assert fake.closed is True


class FailingClosePort(FakePort):
    def close(self) -> None:
        raise OSError("boom")


async def test_on_link_lost_logs_close_failure(caplog):
    core, _ = _core(_fake(FailingClosePort))
    await core.connect_once()
    with caplog.at_level(logging.ERROR):
        core.on_link_lost()
        for _ in range(100):
            if any("closing the lost equipment link failed" in r.message for r in caplog.records):
                break
            await asyncio.sleep(0.01)
    assert any("closing the lost equipment link failed" in r.message for r in caplog.records)


async def test_run_logs_unexpected_error_and_keeps_looping(monkeypatch, caplog):
    fake = _fake()
    connects = []

    def connect():
        connects.append(1)
        return fake

    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) > 6:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(core_mod.asyncio, "sleep", fake_sleep)
    core = EcsCore(LAYOUT, connect=connect)
    real_step = core.step
    steps = []

    def bad_step():
        steps.append(1)
        if len(steps) == 1:
            raise RuntimeError("bug in step")
        real_step()

    monkeypatch.setattr(core, "step", bad_step)
    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        await core.run()
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)
    assert len(connects) == 2  # treated like link loss: reconnected
    assert CONNECT_RETRY_S in sleeps and len(steps) >= 2


class FailingTickPort(FakePort):
    lost = False

    def assign_job(self, equipment_id, job):
        if self.lost:
            raise ConnectionError("link lost")
        return super().assign_job(equipment_id, job)


async def test_submit_connection_error_after_take_still_records_containers():
    fake = _fake(FailingTickPort)
    core, _ = _core(fake)
    await core.connect_once()
    fake.lost = True
    core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])  # no raise: taken
    assert core.is_known("C1")  # the Dispatcher took it: a retry is a duplicate
    assert core.connected is False and core.dispatcher.status()["open"] == 1


class BindFailPort(FakePort):
    closed = False

    def equipment_ids(self):
        raise ConnectionError("lost while binding")

    def close(self) -> None:
        import time as _t

        _t.sleep(0.3)
        self.closed = True


async def test_connect_once_bind_failure_closes_off_loop():
    import time as _t

    fake = _fake(BindFailPort)
    core, _ = _core(fake)
    t0 = _t.monotonic()
    assert await core.connect_once() is False
    assert _t.monotonic() - t0 < 0.2 and fake.closed is False  # not blocking the loop
    for _ in range(100):
        if fake.closed:
            break
        await asyncio.sleep(0.01)
    assert fake.closed is True and core.connected is False


async def test_reconnect_faulted_device_needs_release_then_gets_jobs_again():
    first = _fake(LosingPort)
    second = _fake()  # AGV-01 reports IDLE again on the new link
    ports = [first, second]
    b = Broadcaster()
    q = b.register()
    core = EcsCore(LAYOUT, connect=lambda: ports.pop(0), broadcaster=b)
    await core.connect_once()
    first.set_state("AGV-01", State.FAULT)
    first.lost = True
    core.step()
    assert core.connected is False
    second.advance(90.0)
    _drain(q)
    assert await core.connect_once() is True
    restored = [m for m in _drain(q) if m["type"] == "equipment"]
    assert {m["equipment_id"] for m in restored} == {"AGV-01", "ASC-A", "STS-01"}  # every device
    assert all(m["reason"] == "LINK_RESTORED" and m["sim_time"] == 90.0 for m in restored)
    assert next(m for m in restored if m["equipment_id"] == "AGV-01")["new_state"] == "IDLE"
    # B2d: a device that FAULTed needs an explicit operator release, even though it reports IDLE
    assert core.status()["needs_release"] == 1
    core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])
    assert "AGV-01" not in [eid for eid, _ in second.calls]
    core.dispatcher.release("AGV-01")
    core.submit([WorkOrder("C2", JobType.DISCHARGE, "STS-01", "A/1/1/2")])
    assert "AGV-01" in [eid for eid, _ in second.calls]


async def test_sim_time_going_backwards_flags_and_health_503(caplog):
    import httpx
    from terminal_ecs_service.app import create_app

    first = _fake(LosingPort)
    second = _fake()  # restarted sim: time back at 0
    ports = [first, second]
    core = EcsCore(LAYOUT, connect=lambda: ports.pop(0))
    app = create_app(core, run_loop=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        await core.connect_once()
        first.advance(300.0)
        core.step()
        assert core.time_reversed is False
        assert (await client.get("/health")).status_code == 200
        first.lost = True
        core.step()
        with caplog.at_level(logging.ERROR):
            assert await core.connect_once() is True
        assert core.time_reversed is True
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert errors == ["sim time went backwards (300.0 -> 0.0): restart the ECS service per sim run"]
        r = await client.get("/health")
        assert r.status_code == 503
        assert r.json().items() >= {"connected": True, "sim_time": 0.0, "error": "sim_time_reversed"}.items()
        assert r.json()["rss_mb"] > 0 and r.json()["uptime_s"] >= 0
        second.advance(60.0)
        core.step()  # the loop keeps running
        assert core.connected is True and core.time_reversed is True


async def test_sim_time_going_backwards_detected_in_step(caplog):
    fake = _fake()
    core, _ = _core(fake)
    await core.connect_once()
    fake.advance(120.0)
    core.step()
    fake._now = 10.0  # same link, sim restarted behind it
    with caplog.at_level(logging.ERROR):
        core.step()
        core.step()
    assert core.time_reversed is True and core.connected is True
    assert sum("sim time went backwards" in r.getMessage() for r in caplog.records) == 1


# -- B2d: alarms, release / resolve, auto_release ----------------------------------------
def _alarm_msgs(q, action):
    return [m for m in _drain(q) if m["type"] == "alarm" and m["action"] == action]


async def test_core_link_blip_keeps_orders_and_resumes():
    first = _fake(LosingPort)
    second = _fake()
    ports = [first, second]
    b = Broadcaster()
    q = b.register()
    core = EcsCore(LAYOUT, connect=lambda: ports.pop(0), broadcaster=b)
    await core.connect_once()
    core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])
    (eid, job) = first.calls[0]
    assert eid == "AGV-01" and core.status()["open"] == 1
    _drain(q)
    core.on_link_lost()
    assert core.status()["offline"] == 3 and core.status()["replans"] == 0
    alarms = core.alarms()
    assert {(a["kind"], a["equipment_id"]) for a in alarms} == {
        ("LINK_LOST", "AGV-01"), ("LINK_LOST", "ASC-A"), ("LINK_LOST", "STS-01")}
    assert all(a["job_id"] is None and isinstance(a["since_sim_time"], float) for a in alarms)
    raised = _alarm_msgs(q, "raised")
    assert len(raised) == 3 and all(m["type"] == "alarm" and m["kind"] == "LINK_LOST" for m in raised)
    # the device comes back BUSY with the same job
    second._job["AGV-01"] = job
    second._state["AGV-01"] = State.BUSY
    second.advance(5.0)
    assert await core.connect_once() is True
    assert core.status()["offline"] == 0 and core.alarms() == []
    cleared = _alarm_msgs(q, "cleared")
    assert len(cleared) == 3 and all(m["kind"] == "LINK_LOST" and m["sim_time"] == 5.0 for m in cleared)
    n_calls = len(second.calls)
    second.complete("AGV-01")
    core.step()
    assert len(second.calls) > n_calls  # the order moved on to its next stage on the new link
    assert second.calls[n_calls][1].job_id != job.job_id
    assert core.status()["replans"] == 0


async def test_core_release_and_resolve_publish():
    fake = _fake()
    core, q = _core(fake)
    await core.connect_once()
    fake.set_state("AGV-01", State.FAULT)
    core.publish_alarms()
    raised = _alarm_msgs(q, "raised")
    assert [(m["kind"], m["equipment_id"], m["job_id"]) for m in raised] == [("NEEDS_RELEASE", "AGV-01", None)]
    assert "since_sim_time" in raised[0] and "detail" in raised[0] and "sim_time" in raised[0]
    fake.set_state("AGV-01", State.IDLE)
    assert core.release("AGV-01") is None
    msgs = _drain(q)
    assert [m["action"] for m in msgs if m["type"] == "alarm"] == ["cleared"]
    assert any(m["type"] == "status" for m in msgs)
    assert core.alarms() == []


async def test_core_unknown_outcome_resolve_publishes():
    first = _fake(LosingPort)
    second = _fake()  # AGV-01 returns IDLE without its job: outcome unknown
    ports = [first, second]
    b = Broadcaster()
    q = b.register()
    core = EcsCore(LAYOUT, connect=lambda: ports.pop(0), broadcaster=b)
    await core.connect_once()
    core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])
    job_id = first.calls[0][1].job_id
    core.on_link_lost()
    _drain(q)
    assert await core.connect_once() is True
    raised = _alarm_msgs(q, "raised")
    assert {m["kind"] for m in raised} == {"UNKNOWN_OUTCOME", "NEEDS_RELEASE"}
    assert core.status()["unknown_outcomes"] == 1
    core.resolve_job(job_id, "completed")
    msgs = _drain(q)
    cleared = [m for m in msgs if m["type"] == "alarm" and m["action"] == "cleared"]
    assert [(m["kind"], m["job_id"]) for m in cleared] == [("UNKNOWN_OUTCOME", job_id)]
    assert any(m["type"] == "status" for m in msgs)
    core.release("AGV-01")
    assert core.alarms() == []


async def test_core_publish_alarms_is_a_diff():
    fake = _fake()
    core, q = _core(fake)
    await core.connect_once()
    fake.set_state("AGV-01", State.FAULT)
    core.publish_alarms()
    core.publish_alarms()  # nothing new
    assert len(_alarm_msgs(q, "raised")) == 1


async def test_core_auto_release_passthrough():
    fake = _fake()
    core = EcsCore(LAYOUT, connect=lambda: fake)
    assert core.auto_release is False
    await core.connect_once()
    assert core.dispatcher.auto_release is False
    fake2 = _fake()
    core2 = EcsCore(LAYOUT, connect=lambda: fake2, auto_release=True)
    assert core2.auto_release is True
    await core2.connect_once()
    assert core2.dispatcher.auto_release is True


async def test_core_release_and_resolve_require_connection():
    core, _ = _core(_fake())
    with pytest.raises(RuntimeError, match="not connected"):
        core.release("AGV-01")
    with pytest.raises(RuntimeError, match="not connected"):
        core.resolve_job("J1", "completed")
    assert core.alarms() == []


async def test_core_step_publishes_alarms_from_drained_events():
    fake = _fake()
    core, q = _core(fake)
    await core.connect_once()
    _drain(q)
    fake.set_state("AGV-01", State.FAULT)
    core.step()  # no 60 s boundary crossed
    assert core.tick_count == 0
    raised = _alarm_msgs(q, "raised")
    assert [(m["kind"], m["equipment_id"]) for m in raised] == [("NEEDS_RELEASE", "AGV-01")]
