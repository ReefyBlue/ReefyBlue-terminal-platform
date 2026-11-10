from __future__ import annotations

import logging

from fake_port import FakePort
from terminal_ecs_service.port import ReconnectingPort
from terminal_model.port import EquipmentPort, EventKind
from terminal_model.types import Job, JobType, Kind, Position, State


def _fake() -> FakePort:
    return FakePort({"AGV-01": (Kind.AGV, Position(10, 50)), "STS-01": (Kind.STS, Position(100, 0))})


JOB = Job("J-1", JobType.TRANSPORT, "C1", "AGV-01", "STS-01")


def test_reconnecting_port_swaps_and_redelivers(caplog):
    rp = ReconnectingPort()
    seen = []
    rp.subscribe(seen.append)
    p1 = _fake()
    p1.advance(42.0)
    rp.bind(p1)
    assert rp.connected is True
    p1.set_state("AGV-01", State.BUSY)
    assert len(seen) == 1 and seen[0].kind is EventKind.STATE_CHANGED
    assert rp.snapshot("AGV-01").state is State.BUSY

    rp.unbind()
    assert rp.connected is False and rp.now() == p1.now() == 42.0
    assert rp.equipment_ids() == ["AGV-01", "STS-01"]
    assert rp.snapshot("AGV-01").state is State.BUSY  # last known
    with caplog.at_level(logging.WARNING):
        assert rp.assign_job("AGV-01", JOB) is False
        assert rp.cancel_job("AGV-01") is False
        assert rp.reset("AGV-01") is False
    assert p1.calls == [] and p1.calls_cancel == [] and p1.calls_reset == []
    assert sum("not connected" in r.message for r in caplog.records) == 3
    assert rp.drain_events() == 0

    p1.set_state("AGV-01", State.IDLE)  # stale port: not delivered any more
    assert len(seen) == 1

    p2 = _fake()
    rp.bind(p2)
    p2.set_state("AGV-01", State.IDLE)
    assert len(seen) == 2
    assert rp.assign_job("AGV-01", JOB) is True and p2.calls == [("AGV-01", JOB)]


def test_late_subscriber_is_attached_to_the_bound_port():
    rp = ReconnectingPort()
    p = _fake()
    rp.bind(p)
    seen = []
    rp.subscribe(seen.append)
    p.set_state("STS-01", State.FAULT)
    assert [e.equipment_id for e in seen] == ["STS-01"]


def test_rebinding_the_same_port_does_not_duplicate_events():
    rp = ReconnectingPort()
    seen = []
    rp.subscribe(seen.append)
    p = _fake()
    rp.bind(p)
    rp.unbind()
    rp.bind(p)
    p.set_state("AGV-01", State.BUSY)
    assert len(seen) == 1


def test_drain_events_delegates_when_the_port_has_it():
    class Draining(FakePort):
        def drain_events(self) -> int:
            return 7

    rp = ReconnectingPort()
    rp.bind(Draining({"AGV-01": (Kind.AGV, Position(0, 0))}))
    assert rp.drain_events() == 7
    rp.bind(_fake())
    assert rp.drain_events() == 0


def test_unbound_before_any_bind():
    rp = ReconnectingPort()
    assert rp.connected is False and rp.now() == 0.0 and rp.equipment_ids() == []


def test_reconnecting_port_satisfies_protocol():
    assert isinstance(ReconnectingPort(), EquipmentPort)
