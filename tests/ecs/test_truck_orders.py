"""C1b Task 5: ``TRUCK_IN`` / ``TRUCK_OUT`` work orders in rack mode. The LS-ASC of the block
handles them at its truck head (``TRUCK:<block>:<lane>``) with the sim's fixed cycle; no AGV, no
rack. The WS-ASC does the waterside jobs (STACK_IN / STACK_OUT), the LS-ASC the landside ones and
the housekeeping (STACK_MOVE)."""
from __future__ import annotations

import pytest
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.workorders import WorkOrder, WorkOrderError
from terminal_model.port import EventKind
from terminal_model.types import JobType
from terminal_sim.equipment.asc2 import TRUCK_CYCLE_S
from terminal_sim.loc2 import TruckLoc, parse_loc2
from test_rack_mode import Slots, discharge, make, run_until, where
from test_rehandles import order_jobs, put


def completions(world) -> dict[str, float]:
    done: dict[str, float] = {}
    world.port.subscribe(lambda ev: ev.kind is EventKind.JOB_COMPLETED and done.setdefault(ev.job_id, ev.sim_time))
    return done


def test_truck_in_out_fixed_cycle():
    world, d, _ = make()
    done = completions(world)
    out_slot = put(world, "T2", "A", 38, 4)
    agv_state = {a: (agv.position, agv.loaded_container_id) for a, agv in world.agvs.items()}
    d.submit([
        WorkOrder("T1", JobType.TRUCK_IN, "", "A/38/6/1"),
        WorkOrder("T2", JobType.TRUCK_OUT, "", out_slot),
    ])
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert (d.status()["done"], d.status()["failed"]) == (2, 0)
    (tin,), (tout,) = order_jobs(d, "T1"), order_jobs(d, "T2")
    assert tin.job.type is JobType.TRUCK_IN and tout.job.type is JobType.TRUCK_OUT
    assert tin.equipment_id == tout.equipment_id == "ASC-A-LS"
    src, dst = parse_loc2(tin.job.from_loc), parse_loc2(tout.job.to_loc)
    assert isinstance(src, TruckLoc) and isinstance(dst, TruckLoc) and src.block == dst.block == "A"
    assert tin.job.to_loc == "SLOT:A:37:5" and tout.job.from_loc == "SLOT:A:37:3"
    for lj in (tin, tout):  # the crane's own motions fit in the fixed truck cycle
        assert done[lj.job.job_id] - lj.assigned_at == pytest.approx(TRUCK_CYCLE_S, abs=1.0)
    assert world.stack.locate("T1")[:3] == ("A", 37, 5)
    assert where(world, "T1") == ["stack"]
    assert where(world, "T2") == []  # gone with the truck
    # no AGV and no rack took part
    assert {a: (agv.position, agv.loaded_container_id) for a, agv in world.agvs.items()} == agv_state
    assert all(lj.job.type not in (JobType.TRANSPORT,) or lj.job.job_id.startswith("PARK-")
               for lj in d.job_log.values())
    assert d.alarms() == []


def test_truck_out_under_a_box_is_dug_out_first():
    world, d, _ = make()
    slot = put(world, "T3", "B", 30, 2)
    d.register_stack({"X1": put(world, "X1", "B", 30, 2)})
    d.submit([WorkOrder("T3", JobType.TRUCK_OUT, "", slot)])
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert [(lj.job.type, lj.equipment_id) for lj in order_jobs(d, "T3")] == [
        (JobType.STACK_MOVE, "ASC-B-LS"), (JobType.TRUCK_OUT, "ASC-B-LS")]
    assert d.rehandles("T3") == 1
    assert where(world, "T3") == [] and where(world, "X1") == ["stack"]


def test_ls_asc_does_housekeeping_ws_does_waterside():
    world, d, slots = make()
    load_slot = put(world, "L1", "A", 12, 3)
    d.register_stack({c: put(world, c, "A", 12, 3) for c in ("X1", "X2")})
    out_slot = put(world, "T2", "A", 25, 7)
    d.submit([
        discharge("D1", "STS-01", slots.next_slot("A")),
        WorkOrder("L1", JobType.LOAD, "STS-02", load_slot),
        WorkOrder("T1", JobType.TRUCK_IN, "", "A/30/8/1"),
        WorkOrder("T2", JobType.TRUCK_OUT, "", out_slot),
    ])
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert (d.status()["done"], d.status()["failed"]) == (4, 0)
    role = {JobType.STACK_IN: "WS", JobType.STACK_OUT: "WS",
            JobType.STACK_MOVE: "LS", JobType.TRUCK_IN: "LS", JobType.TRUCK_OUT: "LS"}
    asc_jobs = [lj for lj in d.job_log.values() if lj.job.type in role]
    assert {lj.job.type for lj in asc_jobs} == set(role)
    for lj in asc_jobs:
        assert lj.equipment_id == f"ASC-A-{role[lj.job.type]}", lj.job
    assert d.alarms() == []


def test_truck_orders_are_validated():
    world, d, _ = make()
    with pytest.raises(WorkOrderError) as e:
        d.submit([WorkOrder("T9", JobType.TRUCK_IN, "", "A/1/1")])
    assert e.value.code == "INVALID_SLOT"
    with pytest.raises(WorkOrderError) as e:
        d.submit([WorkOrder("T9", JobType.TRUCK_OUT, "", "Q/1/1/1")])
    assert e.value.code == "INVALID_SLOT"
    legacy = Dispatcher(world.port, Slots())  # no layout: legacy behaviour, trucks unsupported
    with pytest.raises(ValueError, match="unsupported work order type"):
        legacy.submit([WorkOrder("T9", JobType.TRUCK_IN, "", "A/1/1/1")])
