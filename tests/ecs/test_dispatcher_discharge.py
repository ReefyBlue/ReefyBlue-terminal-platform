from __future__ import annotations

import pytest
from fake_port import FakePort, FakeSlots, make_port
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.workorders import WorkOrder, WorkOrderError
from terminal_model.types import Job, JobType, Kind, Position, State


def order(cid: str, slot: str = "A/1/1/1", sts: str = "STS-01") -> WorkOrder:
    return WorkOrder(container_id=cid, type=JobType.DISCHARGE, sts_id=sts, yard_slot=slot)


def test_submit_rejects_duplicate_container():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    with pytest.raises(WorkOrderError) as exc:
        d.submit([order("C1"), order("C1", slot="A/1/1/2")])
    assert exc.value.code == "DUPLICATE_CONTAINER"
    assert port.calls == []
    assert d.status()["pending"] == 0


def test_submit_rejects_duplicate_of_already_known_container():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([order("C1")])
    with pytest.raises(WorkOrderError) as exc:
        d.submit([order("C2"), order("C1")])
    assert exc.value.code == "DUPLICATE_CONTAINER"
    assert len(port.calls) == 1  # only C1's transport; C2 not enqueued


def test_submit_rejects_unknown_sts():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    with pytest.raises(WorkOrderError) as exc:
        d.submit([order("C1"), order("C2", sts="STS-09")])
    assert exc.value.code == "UNKNOWN_STS"
    assert port.calls == []


@pytest.mark.parametrize("slot", ["garbage", "TP/A/WS", "B/1/1/1"])
def test_submit_rejects_invalid_slot(slot):
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    with pytest.raises(WorkOrderError) as exc:
        d.submit([order("C1", slot=slot)])
    assert exc.value.code == "INVALID_SLOT"
    assert port.calls == []


def test_discharge_happy_path():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([order("C1", slot="A/12/3/2")])

    # nearest idle AGV (AGV-01 at (10,50)) is sent to the STS
    assert [(eid, j.job_id, j.type, j.from_loc, j.to_loc) for eid, j in port.calls] == [
        ("AGV-01", "C1-TRANSPORT-1", JobType.TRANSPORT, "AGV-01", "STS-01"),
    ]

    port.complete("AGV-01")
    eid, job = port.calls[-1]
    assert (eid, job.job_id, job.type, job.from_loc, job.to_loc) == (
        "STS-01", "C1-DISCHARGE-1", JobType.DISCHARGE, "VESSEL", "STS-01",
    )

    port.complete("STS-01")
    eid, job = port.calls[-1]
    assert (eid, job.job_id, job.type, job.from_loc, job.to_loc) == (
        "AGV-01", "C1-TRANSPORT-2", JobType.TRANSPORT, "STS-01", "TP/A/WS",
    )

    port.complete("AGV-01")
    eid, job = port.calls[-1]
    assert (eid, job.job_id, job.type, job.from_loc, job.to_loc) == (
        "ASC-A", "C1-STACK_IN-1", JobType.STACK_IN, "TP/A/WS", "A/12/3/2",
    )

    port.complete("ASC-A")
    assert d.completed == ["C1"]
    st = d.status()
    assert st["active_jobs"] == 0
    assert st["done"] == 1
    assert st["pending"] == 0
    assert st["failed"] == 0
    assert d.jobs == {}
    assert len(port.calls) == 4


def test_on_transfer_called_with_exact_tuples():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    transfers: list[tuple[str, str, str]] = []
    d.on_transfer = lambda kind, eid, cid: transfers.append((kind, eid, cid))
    d.submit([order("C1")])
    port.complete("AGV-01")
    assert transfers == []
    port.complete("STS-01")
    assert transfers == [("sts_to_agv", "STS-01", "C1")]
    port.complete("AGV-01")
    assert transfers == [("sts_to_agv", "STS-01", "C1"), ("agv_to_tp", "AGV-01", "C1")]
    port.complete("ASC-A")
    assert len(transfers) == 2


def test_lookahead_limits_agvs_per_sts():
    port = make_port(n_agv=6)
    d = Dispatcher(port, FakeSlots())
    d.submit([order(f"C{i}", slot=f"A/{i}/1/1") for i in range(1, 6)])
    transports = [(eid, j) for eid, j in port.calls if j.type is JobType.TRANSPORT]
    assert len(transports) == 2
    assert all(j.to_loc == "STS-01" for _, j in transports)
    # nearest first: AGV-01 (10,50), then AGV-02 (300,50)
    assert [eid for eid, _ in transports] == ["AGV-01", "AGV-02"]
    assert [j.container_id for _, j in transports] == ["C1", "C2"]
    assert d.status()["pending"] == 3

    # AGV-01 arrives → C1 AT_STS → DISCHARGING (no longer counted) → C3 gets an AGV
    port.complete("AGV-01")
    assert port.calls[-2][1].job_id == "C1-DISCHARGE-1"
    assert (port.calls[-1][0], port.calls[-1][1].job_id) == ("AGV-03", "C3-TRANSPORT-1")
    assert d.status()["pending"] == 2
    # C1 moves to the yard: C2 and C3 still en route → no extra AGV
    port.complete("STS-01")
    assert [j.job_id for _, j in port.calls if j.type is JobType.TRANSPORT][-1] == "C1-TRANSPORT-2"
    assert d.status()["pending"] == 2


def test_slot_occupied_asks_provider():
    port = make_port()
    slots = FakeSlots(["A/2/1/1"])
    d = Dispatcher(port, slots)
    d.submit([order("C1", slot="A/1/1/1")])
    port.complete("AGV-01")
    port.complete("STS-01")
    port.complete("AGV-01")
    port.fail("ASC-A", "SLOT_OCCUPIED")
    assert slots.calls == [("A", "A/1/1/1")]
    eid, job = port.calls[-1]
    assert (eid, job.job_id, job.type, job.from_loc, job.to_loc) == (
        "ASC-A", "C1-STACK_IN-2", JobType.STACK_IN, "TP/A/WS", "A/2/1/1",
    )
    assert d.status()["replans"] == 1
    port.complete("ASC-A")
    assert d.completed == ["C1"]


def test_slot_occupied_and_yard_full_marks_failed():
    port = make_port()
    slots = FakeSlots([])
    d = Dispatcher(port, slots)
    d.submit([order("C1")])
    port.complete("AGV-01")
    port.complete("STS-01")
    port.complete("AGV-01")
    port.fail("ASC-A", "SLOT_OCCUPIED")
    st = d.status()
    assert st["failed"] == 1
    assert st["active_jobs"] == 0
    assert d.completed == []


def test_rejected_assignment_tries_next_agv():
    port = make_port()
    port.assign_job("AGV-01", Job("X", JobType.TRANSPORT, "X", "AGV-01", "STS-01"))
    port.calls.clear()
    # AGV-01 is BUSY now, so the dispatcher must not use it and falls back to AGV-02
    d = Dispatcher(port, FakeSlots())
    d.submit([order("C1")])
    assert [(eid, j.job_id) for eid, j in port.calls] == [("AGV-02", "C1-TRANSPORT-1")]


def test_stack_in_waits_for_busy_asc():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([order("C1", slot="A/1/1/1"), order("C2", slot="A/1/1/2")])
    # C1 via AGV-01, C2 via AGV-02
    port.complete("AGV-01")  # C1 at STS → discharge C1
    port.complete("AGV-02")  # C2 at STS, STS busy
    port.complete("STS-01")  # C1 → yard; C2 discharge starts
    port.complete("AGV-01")  # C1 at TP → STACK_IN C1
    port.complete("STS-01")  # C2 → yard
    port.complete("AGV-02")  # C2 at TP, ASC busy → waits
    assert [j.job_id for e, j in port.calls if e == "ASC-A"] == ["C1-STACK_IN-1"]
    port.complete("ASC-A")
    assert [j.job_id for e, j in port.calls if e == "ASC-A"] == ["C1-STACK_IN-1", "C2-STACK_IN-1"]
    port.complete("ASC-A")
    assert d.completed == ["C1", "C2"]
    assert d.status()["active_jobs"] == 0


def test_subscribes_and_reports_equipment_state():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    assert d.status()["equipment"] == {
        "AGV-01": "IDLE", "AGV-02": "IDLE", "ASC-A": "IDLE", "STS-01": "IDLE",
    }


class EchoPort(FakePort):
    """Emits a STATE_CHANGED event synchronously inside assign_job (like the sim may)."""

    def assign_job(self, equipment_id: str, job: Job) -> bool:
        ok = super().assign_job(equipment_id, job)
        if ok:
            self.set_state(equipment_id, self.snapshot(equipment_id).state)
        return ok


def test_tick_is_reentrancy_safe():
    port = EchoPort({
        "STS-01": (Kind.STS, Position(100, 0)),
        "ASC-A": (Kind.ASC, Position(50, 120)),
        **{f"AGV-{i:02d}": (Kind.AGV, Position(10 * i, 50)) for i in range(1, 7)},
    })
    d = Dispatcher(port, FakeSlots())
    d.submit([order(f"C{i}", slot=f"A/{i}/1/1") for i in range(1, 4)])
    assert [j.job_id for _, j in port.calls] == ["C1-TRANSPORT-1", "C2-TRANSPORT-1"]
    for cid in ("C1", "C2", "C3"):
        agv = next(e for e, j in port.calls if j.job_id == f"{cid}-TRANSPORT-1")
        port.complete(agv)
        port.complete("STS-01")
        port.complete(agv)
        port.complete("ASC-A")
    assert d.completed == ["C1", "C2", "C3"]
    assert len({j.job_id for _, j in port.calls}) == len(port.calls)  # no double issue


def test_discharge_follows_per_sts_fifo_not_arrival_order():
    port = make_port()
    d = Dispatcher(port, FakeSlots())
    d.submit([order("C1", slot="A/1/1/1"), order("C2", slot="A/1/1/2")])
    agv_of = {j.container_id: e for e, j in port.calls}
    assert agv_of == {"C1": "AGV-01", "C2": "AGV-02"}

    port.complete(agv_of["C2"])  # C2 arrives first → must wait in the lane
    assert not [j for _, j in port.calls if j.type is JobType.DISCHARGE]

    port.complete(agv_of["C1"])
    assert port.calls[-1][0] == "STS-01" and port.calls[-1][1].job_id == "C1-DISCHARGE-1"
    assert not [j for _, j in port.calls if j.job_id == "C2-DISCHARGE-1"]

    port.complete("STS-01")
    discharges = [j.job_id for e, j in port.calls if e == "STS-01"]
    assert discharges == ["C1-DISCHARGE-1", "C2-DISCHARGE-1"]


def test_invalid_slot_from_provider_is_treated_as_yard_full():
    port = FakePort({
        "STS-01": (Kind.STS, Position(100, 0)),
        "ASC-A": (Kind.ASC, Position(50, 120)),
        "ASC-B": (Kind.ASC, Position(150, 120)),
        "AGV-01": (Kind.AGV, Position(10, 50)),
    })
    slots = FakeSlots(["B/1/1/1"])  # wrong block
    d = Dispatcher(port, slots)
    d.submit([order("C1", slot="A/1/1/1")])
    port.complete("AGV-01")
    port.complete("STS-01")
    port.complete("AGV-01")
    n_calls = len(port.calls)
    port.fail("ASC-A", "SLOT_OCCUPIED")
    assert slots.calls == [("A", "A/1/1/1")]
    assert len(port.calls) == n_calls  # no STACK_IN-2 to a bogus slot
    st = d.status()
    assert st["failed"] == 1
    assert st["replans"] == 0
    assert st["active_jobs"] == 0


def test_raising_transfer_hook_fails_order_without_stranding_it():
    port = make_port()
    d = Dispatcher(port, FakeSlots())

    def boom(kind: str, eid: str, cid: str) -> None:
        raise RuntimeError("world says no")

    d.on_transfer = boom
    d.submit([order("C1"), order("C2", slot="A/1/1/2")])
    port.complete("AGV-01")
    port.complete("STS-01")  # hook raises for C1 → C1 FAILED, event loop survives
    st = d.status()
    assert st["failed"] == 1
    assert d.completed == []
    assert not [j for _, j in port.calls if j.job_id == "C1-TRANSPORT-2"]
    # dispatcher keeps working: C2 is discharged next
    port.complete("AGV-02")
    assert port.calls[-1][1].job_id == "C2-DISCHARGE-1"


def test_stack_in_assigns_at_most_one_job_per_asc_per_tick():
    class IdleLiarPort(FakePort):
        """ASC keeps reporting IDLE and accepting jobs (worst-case adapter)."""

        def assign_job(self, equipment_id: str, job: Job) -> bool:
            ok = super().assign_job(equipment_id, job)
            if ok and equipment_id == "ASC-A":
                self._state[equipment_id] = State.IDLE
            return ok

    port = IdleLiarPort({
        "STS-01": (Kind.STS, Position(100, 0)),
        "STS-02": (Kind.STS, Position(200, 0)),
        "ASC-A": (Kind.ASC, Position(50, 120)),
        "AGV-01": (Kind.AGV, Position(10, 50)),
        "AGV-02": (Kind.AGV, Position(210, 50)),
    })
    port.assign_job("ASC-A", Job("X", JobType.STACK_IN, "X", "TP/A/WS", "A/9/9/1"))
    port._state["ASC-A"] = State.BUSY  # hold the ASC while both boxes reach the TP
    d = Dispatcher(port, FakeSlots())
    d.submit([order("C1", slot="A/1/1/1"), order("C2", slot="A/1/1/2", sts="STS-02")])
    for agv, sts in (("AGV-01", "STS-01"), ("AGV-02", "STS-02")):
        port.complete(agv)
        port.complete(sts)
        port.complete(agv)
    port._state["ASC-A"] = State.IDLE
    d.tick()
    stack_ins = [j.job_id for e, j in port.calls if e == "ASC-A" and j.container_id != "X"]
    assert stack_ins == ["C1-STACK_IN-1"]
