"""Plan B2c §4.3: job messages come from a drained queue of new jobs (no scan over the
whole job log), and no ``status`` message is built while nobody listens."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fake_port import FakePort, FakeSlots
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.workorders import WorkOrder
from terminal_ecs_service.core import SAMPLE_INTERVAL_S, EcsCore
from terminal_ecs_service.events import Broadcaster
from terminal_model.types import JobType, Kind, Position, State

LAYOUT = SimpleNamespace(blocks=[SimpleNamespace(block="A", bays=40, rows=4, tiers=4)])


def _fake(cls=FakePort) -> FakePort:
    return cls(
        {
            "STS-01": (Kind.STS, Position(100, 0)),
            "AGV-01": (Kind.AGV, Position(10, 50)),
            "ASC-A": (Kind.ASC, Position(50, 120)),
        }
    )


def _slot(i: int) -> str:
    return f"A/{1 + i // 16}/{1 + (i // 4) % 4}/{1 + i % 4}"


def _drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _run_until_done(fake: FakePort, d: Dispatcher, n: int) -> None:
    while d.status()["done"] < n:
        busy = [e for e in fake.equipment_ids() if fake.snapshot(e).state is State.BUSY]
        assert busy, "no progress"
        fake.complete(busy[0])


def test_client_count():
    b = Broadcaster()
    assert b.client_count == 0
    q = b.register()
    b.register()
    assert b.client_count == 2
    b.unregister(q)
    assert b.client_count == 1


def test_new_jobs_since_last_publish_drains_in_order():
    fake = _fake()
    d = Dispatcher(fake, FakeSlots(), auto_release=True)
    d.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])
    fake.complete("AGV-01")
    new = d.new_jobs_since_last_publish()
    assert [j.job.job_id for j in new] == list(d.job_log) == ["C1-TRANSPORT-1", "C1-DISCHARGE-1"]
    assert new[0] is d.job_log["C1-TRANSPORT-1"]
    assert d.new_jobs_since_last_publish() == []


def test_new_jobs_skips_rejected_jobs():
    class Rejecting(FakePort):
        reject = True

        def assign_job(self, equipment_id, job):
            return False if self.reject else super().assign_job(equipment_id, job)

    fake = _fake(Rejecting)
    d = Dispatcher(fake, FakeSlots(), auto_release=True)
    d.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])
    assert d.job_log == {}
    assert d.new_jobs_since_last_publish() == []
    fake.reject = False
    d.tick()
    assert [j.job.job_id for j in d.new_jobs_since_last_publish()] == list(d.job_log)


async def test_publish_new_jobs_is_incremental(monkeypatch):
    fake = _fake()
    b = Broadcaster()
    q = b.register()
    core = EcsCore(LAYOUT, connect=lambda: fake, broadcaster=b)
    await core.connect_once()
    d = core.dispatcher
    n_orders = 334  # 4 jobs each: >= 1 000 logged jobs
    published = 0
    for i in range(n_orders):
        core.submit([WorkOrder(f"C{i}", JobType.DISCHARGE, "STS-01", _slot(i))])
        _run_until_done(fake, d, i + 1)
        core.publish_new_jobs()  # jobs issued from event callbacks (step() does this per tick)
        published += len([m for m in _drain(q) if m["type"] == "job"])  # queue holds 1 000
    assert len(d.job_log) >= 1000
    assert published == len(d.job_log)
    assert d.new_jobs_since_last_publish() == []

    publishing = False
    real_log, real_publish = Dispatcher.job_log, core.publish_new_jobs

    def guarded_log(self):  # the KPI collector may read it; publishing must not
        assert not publishing, "publish_new_jobs read the whole job log"
        return real_log.fget(self)

    def guarded_publish():
        nonlocal publishing
        publishing = True
        try:
            real_publish()
        finally:
            publishing = False

    monkeypatch.setattr(Dispatcher, "job_log", property(guarded_log))
    monkeypatch.setattr(core, "publish_new_jobs", guarded_publish)
    core.submit([WorkOrder("CX", JobType.DISCHARGE, "STS-01", _slot(n_orders))])
    assert [m["job_id"] for m in _drain(q) if m["type"] == "job"] == ["CX-TRANSPORT-1"]
    fake.advance(SAMPLE_INTERVAL_S)
    fake.complete("AGV-01")  # the crane job is issued from the event callback ...
    core.step()  # ... and published by the tick
    jobs = [m for m in _drain(q) if m["type"] == "job"]
    assert jobs == [{"type": "job", "job_id": "CX-DISCHARGE-1", "equipment_id": "STS-01",
                     "sim_time": SAMPLE_INTERVAL_S}]
    assert d.new_jobs_since_last_publish() == []


async def test_publish_status_skips_without_clients(monkeypatch):
    fake = _fake()
    b = Broadcaster()
    core = EcsCore(LAYOUT, connect=lambda: fake, broadcaster=b)
    await core.connect_once()
    calls = []
    real = core.status_message
    monkeypatch.setattr(core, "status_message", lambda: calls.append(1) or real())
    core.publish_status()
    assert calls == []
    q = b.register()
    core.publish_status()
    assert calls == [1] and [m["type"] for m in _drain(q)] == ["status"]
