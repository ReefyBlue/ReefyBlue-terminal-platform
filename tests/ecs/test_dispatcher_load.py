from __future__ import annotations

from fake_port import FakeSlots, make_port
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.workorders import WorkOrder
from terminal_model.port import EquipmentEvent, EventKind
from terminal_model.types import Job, JobType, State


def load(cid: str, slot: str = "A/3/1/1", sts: str = "STS-01") -> WorkOrder:
    return WorkOrder(container_id=cid, type=JobType.LOAD, sts_id=sts, yard_slot=slot)


def discharge(cid: str, slot: str = "A/1/1/1", sts: str = "STS-01") -> WorkOrder:
    return WorkOrder(container_id=cid, type=JobType.DISCHARGE, sts_id=sts, yard_slot=slot)


def last(port) -> tuple[str, str, JobType, str, str]:
    eid, j = port.calls[-1]
    return (eid, j.job_id, j.type, j.from_loc, j.to_loc)


def test_load_happy_path():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    transfers: list[tuple[str, str, str]] = []
    d.on_transfer = lambda kind, eid, cid: transfers.append((kind, eid, cid))
    d.submit([load("C9", slot="A/3/1/1")])

    assert last(port) == ("ASC-A", "C9-STACK_OUT-1", JobType.STACK_OUT, "A/3/1/1", "TP/A/WS")
    assert d.status()["pending"] == 0  # unstacking

    port.complete("ASC-A")  # box at the TP → nearest idle AGV to the block
    assert last(port) == ("AGV-01", "C9-TRANSPORT-1", JobType.TRANSPORT, "AGV-01", "TP/A/WS")
    assert transfers == []

    port.complete("AGV-01")  # AGV picks the box up at the TP
    assert transfers == [("tp_to_agv", "AGV-01", "C9")]
    assert last(port) == ("AGV-01", "C9-TRANSPORT-2", JobType.TRANSPORT, "TP/A/WS", "STS-01")

    port.complete("AGV-01")  # AGV in the crane lane
    assert last(port) == ("STS-01", "C9-LOAD-1", JobType.LOAD, "STS-01", "VESSEL")

    port.complete("STS-01")
    assert transfers == [("tp_to_agv", "AGV-01", "C9"), ("agv_to_sts", "AGV-01", "C9")]
    assert d.completed == ["C9"]
    st = d.status()
    assert (st["active_jobs"], st["done"], st["pending"], st["failed"]) == (0, 1, 0, 0)
    assert d.jobs == {}
    assert len(port.calls) == 4  # STACK_OUT, TRANSPORT x2, LOAD


def test_load_releases_agv_after_loading():
    port = make_port(n_agv=1)
    d = Dispatcher(port, FakeSlots())
    d.submit([load("C1", slot="A/1/1/1"), load("C2", slot="A/2/1/1")])
    for eid in ("ASC-A", "AGV-01", "AGV-01", "STS-01"):
        port.complete(eid)
    assert d.completed == ["C1"]
    port.complete("ASC-A")  # C2 reaches the TP; the single AGV is free again and fetches it
    assert [j.job_id for e, j in port.calls if e == "AGV-01"][-1] == "C2-TRANSPORT-1"


def test_one_box_per_tp_at_a_time():
    port = make_port()
    for agv in ("AGV-01", "AGV-02"):  # no AGV available: C1 has to wait at the TP
        port.assign_job(agv, Job(f"X-{agv}", JobType.TRANSPORT, "X", agv, "STS-01"))
    port.calls.clear()
    d = Dispatcher(port, FakeSlots())
    d.submit([load("C1", slot="A/1/1/1"), load("C2", slot="A/2/1/1")])
    assert [j.job_id for _, j in port.calls] == ["C1-STACK_OUT-1"]

    port.complete("ASC-A")  # C1 sits on the TP (AT_TP), ASC idle
    assert [j.job_id for _, j in port.calls] == ["C1-STACK_OUT-1"]

    port.complete("AGV-01")  # AGV-01 frees up and is sent to the TP (AGV_TO_TP)
    assert port.calls[-1][1].job_id == "C1-TRANSPORT-1"
    assert not [j for _, j in port.calls if j.job_id == "C2-STACK_OUT-1"]

    port.complete("AGV-01")  # box on the AGV: the TP is free again
    assert [j.job_id for e, j in port.calls if e == "ASC-A"] == ["C1-STACK_OUT-1", "C2-STACK_OUT-1"]


def test_stack_out_slot_empty_fails_order():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([load("C1")])
    port.fail("ASC-A", "SLOT_EMPTY")
    st = d.status()
    assert (st["failed"], st["replans"], st["active_jobs"], st["pending"]) == (1, 0, 0, 0)
    assert len(port.calls) == 1  # no retry
    assert d.completed == []


def test_mixed_fifo_per_sts():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([discharge("C1"), load("C2", slot="A/5/1/1")])
    agv_of = {j.container_id: e for e, j in port.calls if j.type is JobType.TRANSPORT}
    assert agv_of == {"C1": "AGV-01"}

    port.complete("ASC-A")  # C2 at the TP → the other AGV fetches it
    c2_agv = port.calls[-1][0]
    assert c2_agv == "AGV-02"
    port.complete(c2_agv)  # TP → AGV
    port.complete(c2_agv)  # AGV in the lane, but C1 is ahead in the STS sequence
    assert not [j for _, j in port.calls if j.type is JobType.LOAD]

    port.complete("AGV-01")  # C1 arrives → discharge first
    assert port.calls[-1][1].job_id == "C1-DISCHARGE-1"
    assert not [j for _, j in port.calls if j.type is JobType.LOAD]

    port.complete("STS-01")  # discharge done → now the load
    sts_jobs = [j.job_id for e, j in port.calls if e == "STS-01"]
    assert sts_jobs == ["C1-DISCHARGE-1", "C2-LOAD-1"]


def test_lookahead_one_keeps_the_lane_for_the_head():
    # Lookahead 1: the single lane slot is kept for the head LOAD that is still being
    # unstacked; a later DISCHARGE filling it would leave the head no place (deadlock).
    port = make_port(n_agv=3)
    d = Dispatcher(port, FakeSlots(), lookahead_per_sts=1)
    d.submit([load("C1", slot="A/5/1/1"), discharge("C2")])
    assert [j.job_id for _, j in port.calls if j.type is JobType.TRANSPORT] == []
    port.complete("ASC-A")  # C1 at the TP: the head gets an AGV
    assert port.calls[-1][1].job_id == "C1-TRANSPORT-1"
    assert "C2-TRANSPORT-1" not in d.jobs


def test_loaded_agv_gets_next_leg_once_idle_without_rejections(caplog):
    # The simulator emits JOB_COMPLETED while the AGV is still BUSY and only then
    # STATE_CHANGED -> IDLE. The next leg must wait for IDLE instead of being
    # offered (and rejected) while the AGV is busy.
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([load("C9", slot="A/3/1/1")])
    port.complete("ASC-A")
    job = port._job["AGV-01"]
    port._job["AGV-01"] = None  # completed, but still BUSY
    caplog.set_level("WARNING", logger="terminal_ecs")
    port._emit(EquipmentEvent(EventKind.JOB_COMPLETED, "AGV-01", port.now(), job_id=job.job_id))
    assert [r.getMessage() for r in caplog.records] == []
    port.set_state("AGV-01", State.IDLE)
    assert last(port) == ("AGV-01", "C9-TRANSPORT-2", JobType.TRANSPORT, "TP/A/WS", "STS-01")


def test_routine_assignments_and_completions_log_at_debug_only(caplog):
    caplog.set_level("INFO", logger="terminal_ecs")
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([load("C9", slot="A/3/1/1")])
    for eid in ("ASC-A", "AGV-01", "AGV-01", "STS-01"):
        port.complete(eid)
    assert d.completed == ["C9"]
    assert [r.getMessage() for r in caplog.records] == []


def test_non_head_load_with_agv_to_tp_counts_against_lookahead():
    # An AGV sent to the TP for a LOAD is already committed to that crane.
    port = make_port(n_agv=6)
    d = Dispatcher(port, FakeSlots(), lookahead_per_sts=2)
    d.submit([discharge("C1"), discharge("C2", slot="A/2/1/1"), load("C3"), discharge("C4", slot="A/4/1/1")])

    def sent() -> dict[str, str]:
        return {j.container_id: eid for eid, j in port.calls if j.type is JobType.TRANSPORT}

    assert set(sent()) == {"C1", "C2"}  # lookahead full
    port.complete("ASC-A")  # C3 at the TP, still blocked by the lookahead
    assert "C3" not in sent()
    port.complete(sent()["C1"])  # C1 under the crane
    port.complete("STS-01")  # C1 discharged -> TO_YARD frees a lookahead place
    assert "C3" in sent()  # non-head C3 (head is C2) gets an AGV: AGV_TO_TP
    d.tick()
    assert "C4" not in sent()  # C2 (en route) + C3 (AGV_TO_TP) fill the lookahead
    port.complete(sent()["C3"])  # C3 picked up -> TO_STS, still counted
    assert "C4" not in sent()


def test_non_head_orders_keep_a_lane_slot_free_for_the_head():
    """Head LOAD waits for a faulted ASC: at most lookahead-1 non-head orders may be
    lane-bound, so the head's AGV still finds a free lane cell when it arrives."""
    port = make_port(n_agv=4)
    d = Dispatcher(port, FakeSlots())  # lookahead_per_sts = 2
    port.set_state("ASC-A", State.FAULT)
    d.submit([load("L1", slot="A/3/1/1"), discharge("D2", slot="A/1/1/1"), discharge("D3", slot="A/1/1/2")])
    sent = [j.container_id for e, j in port.calls if e.startswith("AGV-")]
    assert sent == ["D2"]  # D3 would fill the lane before the head's AGV is there

    port.set_state("ASC-A", State.IDLE)  # head unstacks, its AGV is sent to the TP
    port.complete("ASC-A")
    assert last(port)[1] == "L1-TRANSPORT-1"
    port.complete(last(port)[0])  # box on the AGV, heading for the STS
    port.complete(last(port)[0])  # head AGV in the lane: the slot rule no longer applies
    assert last(port)[1:3] == ("L1-LOAD-1", JobType.LOAD)
    assert "D3" not in [j.container_id for e, j in port.calls if e.startswith("AGV-")]  # lookahead full
