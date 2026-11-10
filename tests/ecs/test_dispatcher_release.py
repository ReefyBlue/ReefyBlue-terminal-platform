"""B2d §5.2/§5.4: every FAULT needs an operator release; alarms and their ``since``."""

from __future__ import annotations

import pytest
from fake_port import FakePort, FakeSlots, make_port
from terminal_ecs.alarms import AlarmKind, ReleaseRefused
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.workorders import WorkOrder
from terminal_model.port import LinkReason
from terminal_model.types import JobType, Kind, Position, State


def order(cid: str, slot: str = "A/1/1/1") -> WorkOrder:
    return WorkOrder(container_id=cid, type=JobType.DISCHARGE, sts_id="STS-01", yard_slot=slot)


def one_agv_port() -> FakePort:
    return FakePort({
        "STS-01": (Kind.STS, Position(100, 0)),
        "ASC-A": (Kind.ASC, Position(50, 120)),
        "AGV-01": (Kind.AGV, Position(10, 50)),
    })


def test_fault_requires_release_before_reuse():
    port = one_agv_port()
    d = Dispatcher(port, FakeSlots())
    port.set_state("AGV-01", State.FAULT)
    port.set_state("AGV-01", State.IDLE)
    d.submit([order("C1")])
    assert port.calls == []
    assert d.status()["needs_release"] == 1
    d.release("AGV-01")
    assert [(e, j.job_id) for e, j in port.calls] == [("AGV-01", "C1-TRANSPORT-1")]
    assert d.status()["needs_release"] == 0 and d.alarms() == []


def test_release_refused_while_fault_or_offline():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    port.set_state("AGV-01", State.FAULT)
    with pytest.raises(ReleaseRefused) as exc:
        d.release("AGV-01")
    assert "FAULT" in exc.value.reason
    port.set_state("AGV-01", State.OFFLINE, LinkReason.LINK_LOST)
    with pytest.raises(ReleaseRefused) as exc:
        d.release("AGV-01")
    assert "OFFLINE" in exc.value.reason
    assert d.status()["needs_release"] == 1


def test_release_refused_with_open_unknown_outcome():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([order("C1")])
    port.set_state("AGV-01", State.OFFLINE, LinkReason.LINK_LOST)
    port.set_state("AGV-01", State.IDLE, LinkReason.LINK_RESTORED)
    with pytest.raises(ReleaseRefused) as exc:
        d.release("AGV-01")
    assert exc.value.reason == "resolve job C1-TRANSPORT-1 first"
    d.resolve_job("C1-TRANSPORT-1", "failed")
    d.release("AGV-01")
    assert d.alarms() == []


def test_release_unknown_equipment_keyerror():
    d = Dispatcher(make_port(), FakeSlots())
    with pytest.raises(KeyError):
        d.release("AGV-99")


def test_auto_release_idle_clears_like_before():
    port = one_agv_port()
    d = Dispatcher(port, FakeSlots(), auto_release=True)
    port.set_state("AGV-01", State.FAULT)
    assert d.status()["needs_release"] == 1
    port.set_state("AGV-01", State.IDLE)
    d.submit([order("C1")])
    assert [(e, j.job_id) for e, j in port.calls] == [("AGV-01", "C1-TRANSPORT-1")]
    assert d.alarms() == []


def test_alarm_since_is_creation_time():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    port.advance(100)
    port.set_state("AGV-01", State.FAULT)
    port.advance(50)
    port.set_state("AGV-01", State.IDLE)
    port.advance(50)
    alarms = d.alarms()
    assert [(a.kind, a.equipment_id, a.since) for a in alarms] == [(AlarmKind.NEEDS_RELEASE, "AGV-01", 100.0)]


def test_status_keeps_existing_keys_first():
    d = Dispatcher(make_port(), FakeSlots())
    assert list(d.status())[-4:] == ["equipment", "offline", "needs_release", "unknown_outcomes"]


def test_needs_release_detail_for_fault():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    port.set_state("AGV-01", State.FAULT)
    assert [a.detail for a in d.alarms()] == ["fault: release required"]
