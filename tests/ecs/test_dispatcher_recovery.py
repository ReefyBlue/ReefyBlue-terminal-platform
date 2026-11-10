from __future__ import annotations

from fake_port import FakePort, FakeSlots, make_port
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.workorders import WorkOrder
from terminal_model.types import Job, JobType, Position, State


def discharge(cid: str, slot: str = "A/1/1/1", sts: str = "STS-01") -> WorkOrder:
    return WorkOrder(container_id=cid, type=JobType.DISCHARGE, sts_id=sts, yard_slot=slot)


def load(cid: str, slot: str = "A/3/1/1", sts: str = "STS-01") -> WorkOrder:
    return WorkOrder(container_id=cid, type=JobType.LOAD, sts_id=sts, yard_slot=slot)


def jobs_of(port, eid: str) -> list[str]:
    return [j.job_id for e, j in port.calls if e == eid]


def test_fault_during_transport_replans():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([discharge("C1")])
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1"]

    port.fail("AGV-01", "FAULT")
    eid, job = port.calls[-1]
    # same leg (empty AGV → STS) on another AGV; the id suffix keeps counting so it never collides
    assert (eid, job.job_id, job.from_loc, job.to_loc) == ("AGV-02", "C1-TRANSPORT-2", "AGV-02", "STS-01")
    st = d.status()
    assert st["replans"] == 1
    assert st["active_jobs"] == 1
    assert "C1-TRANSPORT-1" not in d.jobs

    d.submit([discharge("C2", slot="A/1/1/2")])  # AGV-01 is idle per the port, but excluded
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1"]
    port.set_state("AGV-01", State.IDLE)
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1", "C2-TRANSPORT-1"]


def test_fault_state_change_is_idempotent_with_job_failed():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([discharge("C1")])
    port.set_state("AGV-01", State.FAULT)  # the sim first reports the FAULT state ...
    assert port.calls[-1][0] == "AGV-02"
    port.fail("AGV-01", "FAULT")  # ... then fails the running job
    st = d.status()
    assert st["replans"] == 1
    assert len(port.calls) == 2
    assert d.jobs.keys() == {"C1-TRANSPORT-2"}


def test_late_job_failed_does_not_hit_reissued_job():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([discharge("C1")])
    port.set_state("AGV-01", State.FAULT)
    port.fail("AGV-01", "FAULT")
    port.complete("AGV-02")  # the re-issued job still completes normally
    assert port.calls[-1][1].job_id == "C1-DISCHARGE-1"


def test_watchdog_cancels_stuck_agv():
    port = make_port()
    d = Dispatcher(port, FakeSlots(), watchdog_s=600)
    d.submit([discharge("C1")])
    port.advance(599)
    d.tick()
    assert port.calls_cancel == []

    port.advance(2)  # 601 s without an event or movement
    d.tick()
    assert port.calls_cancel == ["AGV-01"]
    assert port.calls_reset == ["AGV-01"]
    assert d.status()["replans"] == 1
    assert (port.calls[-1][0], port.calls[-1][1].job_id) == ("AGV-02", "C1-TRANSPORT-2")
    d.tick()
    assert port.calls_cancel == ["AGV-01"]  # no double trigger


def test_watchdog_counts_movement_as_progress():
    port = make_port()
    d = Dispatcher(port, FakeSlots(), watchdog_s=600)
    d.submit([discharge("C1")])
    port.advance(500)
    port.set_position("AGV-01", Position(20, 50))
    d.tick()
    port.advance(500)  # 1000 s since assignment, but only 500 s since it moved
    d.tick()
    assert port.calls_cancel == []
    port.advance(101)
    d.tick()
    assert port.calls_cancel == ["AGV-01"]


def test_watchdog_cancel_event_is_not_double_counted():
    class CancellingPort(FakePort):
        """Like the sim: cancel_job emits JOB_FAILED(CANCELLED) synchronously."""

        def cancel_job(self, equipment_id: str) -> bool:
            super().cancel_job(equipment_id)
            self.fail(equipment_id, "CANCELLED")
            return True

    base = make_port()
    port = CancellingPort({e: (base.snapshot(e).kind, base.snapshot(e).position) for e in base.equipment_ids()})
    d = Dispatcher(port, FakeSlots(), watchdog_s=600)
    d.submit([discharge("C1")])
    port.advance(601)
    d.tick()
    assert d.status()["replans"] == 1
    assert [j.job_id for _, j in port.calls] == ["C1-TRANSPORT-1", "C1-TRANSPORT-2"]


def test_box_on_agv_stays_bound():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([discharge("C1")])
    port.complete("AGV-01")
    port.complete("STS-01")  # box on AGV-01, TRANSPORT-2 to the TP
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1", "C1-TRANSPORT-2"]

    port.fail("AGV-01", "BLOCKED")
    assert d.status()["replans"] == 1
    assert d.status()["bound"] == 1  # box on an excluded AGV
    assert jobs_of(port, "AGV-02") == []  # never moved to another AGV
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1", "C1-TRANSPORT-2"]  # excluded until IDLE

    port.set_state("AGV-01", State.IDLE)
    assert d.status()["bound"] == 0
    eid, job = port.calls[-1]
    assert (eid, job.job_id, job.to_loc) == ("AGV-01", "C1-TRANSPORT-3", "TP/A/WS")
    port.complete("AGV-01")
    port.complete("ASC-A")
    assert d.completed == ["C1"]


def test_failed_discharge_is_retried_with_agv_kept_in_lane():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([discharge("C1")])
    port.complete("AGV-01")
    port.fail("STS-01", "FAULT")
    assert jobs_of(port, "STS-01") == ["C1-DISCHARGE-1"]
    port.set_state("STS-01", State.IDLE)
    assert jobs_of(port, "STS-01") == ["C1-DISCHARGE-1", "C1-DISCHARGE-2"]
    port.complete("STS-01")
    assert port.calls[-1][0] == "AGV-01"  # the same AGV waited in the lane


def test_load_rollbacks():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([load("C1")])
    port.fail("ASC-A", "FAULT")  # UNSTACKING → AWAIT_ASC
    port.set_state("ASC-A", State.IDLE)
    assert jobs_of(port, "ASC-A") == ["C1-STACK_OUT-1", "C1-STACK_OUT-2"]
    port.complete("ASC-A")
    port.fail("AGV-01", "FAULT")  # AGV_TO_TP → AT_TP, AGV released
    assert port.calls[-1][0] == "AGV-02"
    port.complete("AGV-02")
    port.fail("AGV-02", "BLOCKED")  # TO_STS: box on the AGV, stays bound
    port.set_state("AGV-02", State.IDLE)
    assert port.calls[-1][0] == "AGV-02" and port.calls[-1][1].to_loc == "STS-01"
    port.complete("AGV-02")
    port.fail("STS-01", "FAULT")  # LOADING → AT_STS_LOAD
    port.set_state("STS-01", State.IDLE)
    assert jobs_of(port, "STS-01") == ["C1-LOAD-1", "C1-LOAD-2"]
    port.complete("STS-01")
    assert d.completed == ["C1"]
    assert d.status()["replans"] == 4


def test_fifo_head_requeued_after_agv_fault():
    port = make_port(n_agv=4)
    d = Dispatcher(port, FakeSlots())  # lookahead 2
    d.submit([discharge(f"C{i}", slot=f"A/{i}/1/1") for i in (1, 2, 3)])
    agv_of = {j.container_id: e for e, j in port.calls}
    assert agv_of == {"C1": "AGV-01", "C2": "AGV-02"}

    port.complete("AGV-02")  # C2 waits in the lane behind head C1
    port.fail("AGV-01", "FAULT")  # head loses its AGV → requeued as head
    eid, job = port.calls[-1]
    assert (job.container_id, job.to_loc) == ("C1", "STS-01")
    new_agv = eid
    assert new_agv not in ("AGV-01", "AGV-02")
    assert not [j for _, j in port.calls if j.container_id == "C3"]  # lane full
    assert not [j for _, j in port.calls if j.type is JobType.DISCHARGE]  # C2 still waits

    port.complete(new_agv)
    assert [j.job_id for e, j in port.calls if e == "STS-01"] == ["C1-DISCHARGE-1"]


def test_transfer_error_quarantines_agv():
    port = make_port()
    d = Dispatcher(port, FakeSlots())

    def hook(kind: str, eid: str, cid: str) -> None:
        if cid == "C1":
            raise RuntimeError("box lost")

    d.on_transfer = hook
    d.submit([discharge("C1"), discharge("C2", slot="A/1/1/2")])
    port.complete("AGV-01")
    port.complete("STS-01")  # sts_to_agv raises for C1 → FAILED, AGV-01 quarantined
    assert d.status()["failed"] == 1

    d.submit([discharge("C3", slot="A/1/1/3")])
    port.set_state("AGV-01", State.IDLE)  # IDLE does not lift a quarantine
    assert jobs_of(port, "AGV-01") == ["C1-TRANSPORT-1"]
    assert d.status()["equipment"]["AGV-01"] == State.IDLE

    for eid in ("AGV-02", "STS-01", "AGV-02", "ASC-A"):  # finish C2, AGV-02 is freed
        port.complete(eid)
    assert d.completed == ["C2"]
    assert [j.job_id for _, j in port.calls if j.container_id == "C3"] == ["C3-TRANSPORT-1"]
    assert port.calls[-1][0] == "AGV-02"



def test_rolled_back_order_keeps_its_position():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([load("C1", slot="A/1/1/1"), load("C2", slot="A/2/1/1")])
    port.fail("ASC-A", "FAULT")  # C1 back to AWAIT_ASC, next to the waiting C2
    port.set_state("ASC-A", State.IDLE)
    assert jobs_of(port, "ASC-A") == ["C1-STACK_OUT-1", "C1-STACK_OUT-2"]


def test_head_load_is_not_starved_of_agvs():
    # Reviewer repro: two AGVs, head LOAD still unstacking; later discharges must leave one AGV free.
    port = make_port(n_agv=2)
    d = Dispatcher(port, FakeSlots())
    d.submit([load("C1", slot="A/5/1/1"), discharge("C2", slot="A/1/1/1"), discharge("C3", slot="A/1/1/2")])
    transports = [(e, j.job_id) for e, j in port.calls if j.type is JobType.TRANSPORT]
    assert transports == [("AGV-01", "C2-TRANSPORT-1")]  # AGV-02 kept in reserve for C1
    port.complete("ASC-A")  # C1 at the TP
    port.complete("AGV-01")  # C2 parks in the lane behind C1
    assert (port.calls[-1][0], port.calls[-1][1].job_id) == ("AGV-02", "C1-TRANSPORT-1")

    for _ in range(40):  # drive to completion: complete whatever is running
        busy = sorted({e for e, j in port.calls if j.job_id in d.jobs})
        if not busy:
            break
        port.complete(busy[0])
    assert d.completed[0] == "C1"
    assert sorted(d.completed) == ["C1", "C2", "C3"]
    assert d.status()["active_jobs"] == 0


def test_crane_does_not_serve_faulted_lane_agv():
    port = make_port()
    port.assign_job("STS-01", Job("X", JobType.DISCHARGE, "X", "VESSEL", "STS-01"))  # crane busy
    port.calls.clear()
    d = Dispatcher(port, FakeSlots())
    d.submit([discharge("C1")])
    port.complete("AGV-01")  # C1 AT_STS, waits for the crane
    port.set_state("AGV-01", State.FAULT)  # lane AGV faults (no running job)
    port.complete("STS-01")  # crane free
    assert jobs_of(port, "STS-01") == []
    port.set_state("AGV-01", State.IDLE)
    assert jobs_of(port, "STS-01") == ["C1-DISCHARGE-1"]


def test_open_counts_orders_parked_behind_a_faulted_crane():
    """An order waiting for a faulted STS has no job and is not pending, but is open."""
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([discharge("C1")])
    port.complete("AGV-01")  # AGV in the lane -> DISCHARGE issued
    port.set_state("STS-01", State.FAULT)  # crane job rolled back, STS excluded
    st = d.status()
    assert (st["pending"], st["active_jobs"], st["open"]) == (0, 0, 1)
