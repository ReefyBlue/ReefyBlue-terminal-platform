"""B2d §5.1/§5.3: OFFLINE does not replan; return handling; unknown outcomes."""

from __future__ import annotations

import logging

import pytest
from fake_port import FakeSlots, make_port
from terminal_ecs.alarms import Alarm, AlarmKind
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.workorders import WorkOrder
from terminal_model.port import EquipmentEvent, EventKind, LinkReason
from terminal_model.types import JobType, State

LOST, RESTORED = LinkReason.LINK_LOST, LinkReason.LINK_RESTORED


def order(cid: str, slot: str = "A/1/1/1") -> WorkOrder:
    return WorkOrder(container_id=cid, type=JobType.DISCHARGE, sts_id="STS-01", yard_slot=slot)


def jobs_of(port, eid: str) -> list[str]:
    return [j.job_id for e, j in port.calls if e == eid]


def kinds(d: Dispatcher) -> set[tuple[AlarmKind, str, str | None]]:
    return {(a.kind, a.equipment_id, a.job_id) for a in d.alarms()}


def test_offline_does_not_replan_and_blocks_dispatch():
    port = make_port(n_agv=3)
    d = Dispatcher(port, FakeSlots())
    d.submit([order("C1")])
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1"]

    port.set_state("AGV-01", State.OFFLINE, LOST)
    s = d.status()
    assert (s["replans"], s["offline"], s["needs_release"], s["unknown_outcomes"]) == (0, 1, 0, 0)
    assert "C1-TRANSPORT-1" in d.jobs
    assert kinds(d) == {(AlarmKind.LINK_LOST, "AGV-01", None)}

    # even if a cached snapshot still says IDLE, the offline AGV gets no new job
    port._state["AGV-01"] = State.IDLE
    port._job["AGV-01"] = None
    d.submit([order("C2", slot="A/1/1/2")])
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1"]
    assert jobs_of(port, "AGV-02") == ["C2-TRANSPORT-1"]


def test_offline_busy_return_keeps_job_without_watchdog():
    port = make_port()
    d = Dispatcher(port, FakeSlots(), watchdog_s=600.0)
    d.submit([order("C1")])
    port.set_state("AGV-01", State.OFFLINE, LOST)
    port.advance(1200)
    for _ in range(3):
        d.tick()
    assert port.calls_cancel == [] and d.status()["replans"] == 0

    port.set_state("AGV-01", State.BUSY, RESTORED)
    assert d.status()["offline"] == 0 and d.alarms() == []
    assert "C1-TRANSPORT-1" in d.jobs
    port.advance(100)
    d.tick()
    assert port.calls_cancel == []

    port.complete("AGV-01")  # the leg finishes: the order goes on to the crane
    assert jobs_of(port, "STS-01") == ["C1-DISCHARGE-1"]


def test_offline_idle_return_with_job_is_unknown_outcome():
    port = make_port()
    d = Dispatcher(port, FakeSlots(), watchdog_s=600.0)
    d.submit([order("C1")])
    port.advance(10)
    port.set_state("AGV-01", State.OFFLINE, LinkReason.UPSTREAM_DISCONNECTED)
    port.advance(5)
    port.set_state("AGV-01", State.IDLE, LinkReason.UPSTREAM_RECONNECTED)

    s = d.status()
    assert (s["offline"], s["unknown_outcomes"], s["needs_release"], s["replans"]) == (0, 1, 1, 0)
    assert d.alarms() == [
        Alarm(AlarmKind.NEEDS_RELEASE, "AGV-01", None, 15.0, "unknown outcome: release required"),
        Alarm(AlarmKind.UNKNOWN_OUTCOME, "AGV-01", "C1-TRANSPORT-1", 15.0,
              "outcome of C1-TRANSPORT-1 unknown after link loss"),
    ]
    assert "C1-TRANSPORT-1" in d.jobs  # the order keeps its stage until resolved

    port.advance(1200)  # no watchdog on a job whose outcome is unknown
    d.tick()
    assert port.calls_cancel == []
    d.submit([order("C2", slot="A/1/1/2")])
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1"]  # not dispatched before release


def test_offline_idle_return_without_job_is_available():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    port.set_state("AGV-01", State.OFFLINE, LOST)
    port.set_state("AGV-01", State.IDLE, RESTORED)
    assert d.alarms() == [] and d.status()["needs_release"] == 0
    d.submit([order("C1")])
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1"]


def test_offline_fault_return_replans_and_needs_release():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([order("C1")])
    port.set_state("AGV-01", State.OFFLINE, LOST)
    port.set_state("AGV-01", State.FAULT, RESTORED)
    s = d.status()
    assert (s["offline"], s["replans"], s["needs_release"], s["unknown_outcomes"]) == (0, 1, 1, 0)
    assert jobs_of(port, "AGV-02") == ["C1-TRANSPORT-2"]
    assert kinds(d) == {(AlarmKind.NEEDS_RELEASE, "AGV-01", None)}


def test_offline_return_still_offline_stays_offline():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    port.set_state("AGV-01", State.OFFLINE, LOST)
    port.advance(30)
    port.set_state("AGV-01", State.OFFLINE, LinkReason.UPSTREAM_RECONNECTED)
    assert d.status()["offline"] == 1
    assert d.alarms()[0].since == 0.0


def test_completed_during_offline_clears_offline_job():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([order("C1")])
    port.set_state("AGV-01", State.OFFLINE, LOST)
    port.complete("AGV-01")  # cannot arrive via a real port, but is processed normally
    assert "C1-TRANSPORT-1" not in d.jobs
    assert jobs_of(port, "STS-01") == []  # the crane does not serve an AGV it cannot see
    port.set_state("AGV-01", State.IDLE, RESTORED)
    assert d.status()["unknown_outcomes"] == 0 and d.alarms() == []
    assert jobs_of(port, "STS-01") == ["C1-DISCHARGE-1"]


def test_offline_event_for_unknown_equipment_ignored(caplog):
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    with caplog.at_level(logging.WARNING, logger="terminal_ecs"):
        port.emit(EquipmentEvent(EventKind.STATE_CHANGED, "AGV-99", 0.0, reason=LOST, new_state=State.OFFLINE))
    assert d.status()["offline"] == 0 and d.alarms() == []
    assert any("AGV-99" in r.getMessage() for r in caplog.records)


def _unknown_discharge(port, d):
    """C1's DISCHARGE was running on STS-01 when its link dropped; it returns IDLE."""
    d.submit([order("C1")])
    port.complete("AGV-01")
    assert jobs_of(port, "STS-01") == ["C1-DISCHARGE-1"]
    port.set_state("STS-01", State.OFFLINE, LOST)
    port.set_state("STS-01", State.IDLE, RESTORED)
    assert d.status()["unknown_outcomes"] == 1


def test_resolve_completed_advances_stage():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    _unknown_discharge(port, d)
    d.resolve_job("C1-DISCHARGE-1", "completed")
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1", "C1-TRANSPORT-2"]  # TO_YARD leg
    s = d.status()
    assert (s["unknown_outcomes"], s["needs_release"], s["replans"]) == (0, 1, 0)
    d.release("STS-01")
    assert d.alarms() == []


def test_resolve_failed_replans():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    _unknown_discharge(port, d)
    d.resolve_job("C1-DISCHARGE-1", "failed")
    s = d.status()
    assert (s["unknown_outcomes"], s["needs_release"], s["replans"]) == (0, 1, 1)
    assert jobs_of(port, "STS-01") == ["C1-DISCHARGE-1"]  # crane not used before release
    d.release("STS-01")
    assert jobs_of(port, "STS-01") == ["C1-DISCHARGE-1", "C1-DISCHARGE-2"]


def test_resolve_unknown_job_keyerror():
    d = Dispatcher(make_port(), FakeSlots())
    with pytest.raises(KeyError):
        d.resolve_job("C9-TRANSPORT-1", "failed")


def test_resolve_bad_outcome_valueerror():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    _unknown_discharge(port, d)
    with pytest.raises(ValueError):
        d.resolve_job("C1-DISCHARGE-1", "maybe")  # type: ignore[arg-type]
    assert d.status()["unknown_outcomes"] == 1


def test_auto_release_resolves_unknown_as_completed():
    port = make_port()
    d = Dispatcher(port, FakeSlots(), auto_release=True)
    d.submit([order("C1")])
    port.set_state("AGV-01", State.OFFLINE, LOST)
    port.set_state("AGV-01", State.IDLE, RESTORED)
    s = d.status()
    assert (s["replans"], s["unknown_outcomes"], s["needs_release"], s["offline"]) == (0, 0, 0, 0)
    assert d.alarms() == []
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1"]  # no re-issue: the leg counts as done
    assert jobs_of(port, "STS-01") == ["C1-DISCHARGE-1"]  # order advanced AGV_EN_ROUTE -> AT_STS


def test_fault_after_unknown_outcome_keeps_job_for_operator():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    _unknown_discharge(port, d)
    port.set_state("STS-01", State.FAULT)  # the unknown job is not replanned behind the operator's back
    assert d.status()["replans"] == 0 and "C1-DISCHARGE-1" in d.jobs
    port.set_state("STS-01", State.IDLE)
    d.resolve_job("C1-DISCHARGE-1", "completed")
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1", "C1-TRANSPORT-2"]  # order advanced to TO_YARD
    s = d.status()
    assert (s["replans"], s["unknown_outcomes"], s["needs_release"]) == (0, 0, 1)
    assert [a.detail for a in d.alarms()] == ["unknown outcome: release required"]


def test_bound_counts_offline_agv_with_box():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([order("C1")])
    port.complete("AGV-01")  # AGV in the lane; crane discharges onto it
    assert jobs_of(port, "STS-01") == ["C1-DISCHARGE-1"]
    port.set_state("AGV-01", State.OFFLINE, LOST)
    port.complete("STS-01")  # box now on the offline AGV: TO_YARD waits for it
    assert d.status()["bound"] == 1
    port.set_state("AGV-01", State.IDLE, RESTORED)
    assert d.status()["bound"] == 0
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1", "C1-TRANSPORT-2"]
