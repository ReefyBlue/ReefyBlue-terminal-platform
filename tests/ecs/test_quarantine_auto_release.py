"""Plan B2c §4.2: under ``auto_release`` a quarantine is lifted when the AGV reports IDLE
with no box on board (``extra["loaded_container_id"]`` empty); with a box, or without
``auto_release``, it stays until an operator ``release()``."""

from __future__ import annotations

import pytest
from fake_port import FakePort, FakeSlots, make_dispatcher
from terminal_ecs.workorders import WorkOrder
from terminal_model.port import LinkReason
from terminal_model.types import JobType, Kind, Position, State


def discharge(cid: str, slot: str = "A/1/1/1", sts: str = "STS-01") -> WorkOrder:
    return WorkOrder(cid, JobType.DISCHARGE, sts, slot)


def jobs_of(port: FakePort, eid: str) -> list[str]:
    return [j.job_id for e, j in port.calls if e == eid]


def _quarantined_agv(*, auto_release: bool):
    """AGV-01 quarantined by a failed hand-over (TRANSFER_ERROR); C2 waits for an AGV."""
    port = FakePort({  # a single AGV: C2 can only go to AGV-01
        "STS-01": (Kind.STS, Position(100, 0)),
        "ASC-A": (Kind.ASC, Position(50, 120)),
        "AGV-01": (Kind.AGV, Position(10, 50)),
    })
    d = make_dispatcher(port, FakeSlots(), auto_release=auto_release)

    def hook(kind: str, eid: str, cid: str) -> None:
        raise RuntimeError("box lost")

    d.on_transfer = hook
    d.submit([discharge("C1")])
    port.complete("AGV-01")
    port.complete("STS-01")  # sts_to_agv raises: C1 FAILED, AGV-01 quarantined
    d.on_transfer = None
    assert "AGV-01" in d._quarantined and d.status()["needs_release"] == 1
    d.submit([discharge("C2", slot="A/1/1/2")])
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1"]  # no job while quarantined
    return port, d


@pytest.mark.parametrize("empty", ["", None])
def test_quarantine_without_box_lifts_under_auto_release(empty, caplog):
    port, d = _quarantined_agv(auto_release=True)
    port.set_extra("AGV-01", loaded_container_id=empty)  # the box was taken off
    port.set_state("AGV-01", State.IDLE)
    assert "AGV-01" not in d._quarantined
    assert d.status()["needs_release"] == 0 and d.alarms() == []
    assert jobs_of(port, "AGV-01")[-1] == "C2-TRANSPORT-1"
    assert "quarantine of AGV-01 lifted by auto_release" in caplog.text


def test_quarantine_without_box_lifts_when_link_returns_idle():
    port, d = _quarantined_agv(auto_release=True)
    port.set_state("AGV-01", State.OFFLINE, reason=LinkReason.LINK_LOST)
    port.set_extra("AGV-01", loaded_container_id="")
    port.set_state("AGV-01", State.IDLE, reason=LinkReason.LINK_RESTORED)
    assert "AGV-01" not in d._quarantined and d.alarms() == []
    assert jobs_of(port, "AGV-01")[-1] == "C2-TRANSPORT-1"


def test_quarantine_with_box_stays_under_auto_release():
    port, d = _quarantined_agv(auto_release=True)
    port.set_extra("AGV-01", loaded_container_id="C1")
    port.set_state("AGV-01", State.IDLE)
    assert "AGV-01" in d._quarantined and d.status()["needs_release"] == 1
    assert [a.detail for a in d.alarms()] == ["quarantined: box may be on board: release required"]
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1"]


def test_quarantine_stays_when_load_is_not_reported():
    """No ``loaded_container_id`` in the snapshot: unknown, not empty — keep it."""
    port, d = _quarantined_agv(auto_release=True)
    port.set_state("AGV-01", State.IDLE)
    assert "AGV-01" in d._quarantined and d.status()["needs_release"] == 1


def test_quarantine_stays_without_auto_release():
    port, d = _quarantined_agv(auto_release=False)
    port.set_extra("AGV-01", loaded_container_id="")
    port.set_state("AGV-01", State.IDLE)
    assert "AGV-01" in d._quarantined and d.status()["needs_release"] == 1
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1"]
    d.release("AGV-01")  # the operator still can
    assert jobs_of(port, "AGV-01")[-1] == "C2-TRANSPORT-1"
