"""World2: factory, public surface, the discharge round trip and the lane buffer (Review Focus 4)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from terminal_model.locations import SlotLoc
from terminal_model.port import EquipmentPort, EventKind
from terminal_model.types import Job, JobType, State
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_sim.world import World, make_world
from terminal_sim.world2 import World2

ROOT = Path(__file__).resolve().parents[2]


def test_make_world_chooses_by_layout(world2):
    legacy = make_world(load_layout(ROOT / "terminal.yaml"),
                        load_scenario(ROOT / "scenarios" / "baseline.yaml"))
    assert type(legacy) is World
    assert isinstance(world2, World2)
    assert load_layout(ROOT / "terminal.yaml").terminal is None


def test_world2_surface_and_equipment_order(world2):
    assert isinstance(world2.port, EquipmentPort)
    ids = world2.port.equipment_ids()
    assert ids[:3] == ["STS-01", "STS-02", "STS-03"]
    assert ids[3:15] == [f"AGV-{i:02d}" for i in range(1, 13)]
    assert ids[15:] == [i for b in "ABCD" for i in (f"ASC-{b}-WS", f"ASC-{b}-LS")]
    assert world2.now() == 0 and world2.faults_total() == 0
    for eid in ids:
        snap = world2.port.snapshot(eid)
        assert snap.state is State.IDLE
        json.dumps(dict(snap.extra))
    assert "block_id" in world2.port.snapshot("ASC-A-LS").extra
    assert world2.port.snapshot("AGV-01").extra["loaded_container_id"] is None
    world2.run(10)
    assert world2.now() == 10


def test_scenario_fleet_beyond_layout_is_rejected(tmp_path):
    layout = load_layout(ROOT / "terminal_mv_small.yaml")
    raw = (ROOT / "tests/e2e/fixtures/mv_small_baseline.yaml").read_text()
    p = tmp_path / "s.yaml"
    p.write_text(raw.replace("n_agv: 12", "n_agv: 13"))
    with pytest.raises(ValueError):
        load_scenario(p, layout)


def test_yard_adapter_stacks_on_top(world2):
    yard = world2.yard
    assert yard.is_free(SlotLoc("A", 1, 1, 1))
    yard.occupy(SlotLoc("A", 1, 1, 1), "C1")
    with pytest.raises(ValueError):
        yard.occupy(SlotLoc("A", 2, 1, 2), "C2")  # tier 2 on an empty column
    yard.occupy(SlotLoc("A", 1, 1, 2), "C2")
    assert yard.locate("C2") == SlotLoc("A", 1, 1, 2)
    assert world2.stack.locate("C2") == ("A", 0, 0, 1)
    assert not yard.is_free(SlotLoc("A", 1, 1, 2))
    assert yard.release(SlotLoc("A", 1, 1, 2)) == "C2"


def test_faults_attach_to_v2_equipment(make_world2):
    from terminal_model.types import Kind
    from terminal_sim.scenario import FaultParams
    w = make_world2(faults={k: FaultParams(600.0, 60.0) for k in Kind})
    assert len(w.fault_injectors) == len(w.equipment)
    w.run(5000)
    assert w.faults_total() > 0


def test_world2_discharge_roundtrip(world2):
    """One container vessel -> lane -> AGV -> rack -> stack, scripted on the port's events."""
    w = world2
    times: dict[str, float] = {}
    rack_arrival: list[float] = []

    def on_event(e):
        if e.kind is EventKind.JOB_FAILED:
            raise AssertionError(f"{e.equipment_id} {e.job_id} failed: {e.reason}")
        if e.kind is not EventKind.JOB_COMPLETED:
            return
        times[e.job_id] = e.sim_time
        if e.job_id == "T1":  # AGV in the lane: the crane may discharge
            assert w.port.assign_job("STS-01", Job("D1", JobType.DISCHARGE, "C1",
                                                   "VESSEL:A:6:11:12", "LANE:STS-01-L1"))
            times["D1_start"] = e.sim_time
        elif e.job_id == "D1":
            assert w.port.assign_job("AGV-01", Job("T2", JobType.TRANSPORT, "C1",
                                                   "LANE:STS-01-L1", "RACK:RK-A-1"))
        elif e.job_id == "T2":
            assert w.port.assign_job("ASC-A-WS", Job("S1", JobType.STACK_IN, "C1",
                                                     "RACK:RK-A-1", "SLOT:A:5:3"))
            times["S1_start"] = e.sim_time

    w.port.subscribe(on_event)
    assert w.port.assign_job("AGV-01", Job("T1", JobType.TRANSPORT, "C1", "BUFFER:BUF-01-1",
                                           "LANE:STS-01-L1"))
    agv = w.equipment["AGV-01"]
    t = 0.0
    while "S1" not in times and t < 3000:
        t += 0.5
        w.run(t)
        if not rack_arrival and agv.at_point == "RK-A-1":
            rack_arrival.append(t)
    sts_move = times["D1"] - times["D1_start"]
    rack_op = times["T2"] - rack_arrival[0]
    asc_cycle = times["S1"] - times["S1_start"]
    print(f"roundtrip: AGV to lane {times['T1']:.1f} s, STS move {sts_move:.1f} s, "
          f"AGV lane->rack {times['T2'] - times['D1']:.1f} s (rack op {rack_op:.1f} s), "
          f"ASC STACK_IN {asc_cycle:.1f} s, total {times['S1']:.1f} s")
    assert 60 <= sts_move <= 140
    assert rack_op == pytest.approx(20.0, abs=0.5)
    assert 60 <= asc_cycle <= 300
    assert w.stack.locate("C1") == ("A", 5, 3, 0)
    assert w.racks.occupant("RK-A-1") is None
    assert agv.loaded_container_id is None
    assert w.equipment["STS-01"].extra()["wait_for_agv_s"] == 0


def test_sts_uses_lane_buffer_when_agv_late(world2):
    """Review Focus 4: the AGV is 2 minutes late. The STS sets the box in the lane buffer and
    goes on with the next move without waiting; the late AGV lifts the box from the buffer."""
    w = world2
    done: dict[str, float] = {}
    waits: dict[str, float] = {}
    started: dict[str, float] = {}
    sts = w.equipment["STS-01"]

    def on_event(e):
        if e.kind is EventKind.JOB_COMPLETED:
            done[e.job_id] = e.sim_time
            if e.equipment_id == "STS-01":
                waits[e.job_id] = sts.extra()["wait_for_agv_s"]
        elif (e.kind is EventKind.STATE_CHANGED and e.equipment_id == "STS-01"
              and e.new_state is State.IDLE and "D1" in done and "D2" not in started):
            started["D2"] = e.sim_time
            assert w.port.assign_job("STS-01", Job("D2", JobType.DISCHARGE, "C2",
                                                   "VESSEL:A:6:3:14", "LANE:*"))

    w.port.subscribe(on_event)
    w.port.assign_job("STS-01", Job("D1", JobType.DISCHARGE, "C1", "VESSEL:A:6:11:12",
                                    "LANE:STS-01-L1"))
    w.run(140.0)
    assert "D1" in done and sts.lane_buffer["STS-01-L1"] == "C1"
    late = done["D1"] + 120.0
    w.run(late)
    assert w.port.assign_job("AGV-01", Job("T1", JobType.TRANSPORT, "C1", "BUFFER:BUF-01-1",
                                           "LANE:STS-01-L1"))
    w.run(late + 300)
    assert waits == {"D1": 0.0, "D2": 0.0}
    assert started["D2"] == done["D1"]  # straight on with the next move
    assert done["D2"] - done["D1"] < 140
    assert sts.lane_buffer == {"STS-01-L1": None, "STS-01-L2": "C2", "STS-01-L3": None}
    assert w.equipment["AGV-01"].loaded_container_id == "C1"
    assert done["T1"] > late


# --- handover invariants (review fixes) ------------------------------------------------------
def _where(w, cid):
    """Every place a box can be in World2."""
    places = []
    if w.stack.locate(cid) is not None:
        places.append("stack")
    places += [f"rack:{r}" for r in w.rack_specs if w.racks.occupant(r) == cid]
    places += [f"agv:{a.equipment_id}" for a in w.agvs.values() if a.carries(cid)]
    for s in w.sts:
        places += [f"lane:{ln}" for ln, c in s.lane_buffer.items() if c == cid]
        if s.holding == cid:
            places.append(f"sts:{s.equipment_id}")
    places += [f"asc:{e.equipment_id}" for e in w.equipment.values()
               if getattr(e, "role", None) and e.holding == cid]
    places += ["vessel"] * sum(v.loaded.count(cid) for v in w.vessels.values())
    return places


def test_rack_booked_by_stack_out_refuses_the_agv_drop(make_world2, rec):
    w = make_world2(n_agv=1)
    r = rec(w)
    w.stack.put("A", 0, 0, "C1")
    w.equipment["AGV-01"].load("C2")
    assert w.port.assign_job("ASC-A-WS", Job("S1", JobType.STACK_OUT, "C1", "SLOT:A:0:0", "RACK:RK-A-1"))
    w.run(15)  # the AGV comes 15 s later, long before the crane sets C1 down
    assert w.port.assign_job("AGV-01", Job("T1", JobType.TRANSPORT, "C2", "BUFFER:BUF-01-1", "RACK:RK-A-1"))
    w.run(2000)  # must not raise out of the sim
    assert r.failed("AGV-01", "T1") == "RACK_OCCUPIED"
    assert r.done("ASC-A-WS", "S1") is not None
    assert _where(w, "C1") == ["rack:RK-A-1"] and _where(w, "C2") == ["agv:AGV-01"]
    assert w.rack_res == {}


def test_rack_booked_by_the_agv_refuses_the_stack_out(make_world2, rec):
    w = make_world2(n_agv=1)
    r = rec(w)
    w.stack.put("A", 0, 0, "C1")
    w.equipment["AGV-01"].load("C2")
    w.port.assign_job("AGV-01", Job("T1", JobType.TRANSPORT, "C2", "BUFFER:BUF-01-1", "RACK:RK-A-1"))
    t = 0.0
    while w.rack_res.get("RK-A-1") != "AGV-01":
        t += 0.5
        w.run(t)
    w.port.assign_job("ASC-A-WS", Job("S1", JobType.STACK_OUT, "C1", "SLOT:A:0:0", "RACK:RK-A-1"))
    w.run(t + 2000)
    assert r.failed("ASC-A-WS", "S1") == "RACK_OCCUPIED"
    assert r.done("AGV-01", "T1") is not None
    assert _where(w, "C1") == ["stack"] and _where(w, "C2") == ["rack:RK-A-1"]


def _fault(w, eid, until_cond, step=0.5, limit=3000.0):
    eq = w.equipment[eid]
    t = w.now()
    while not until_cond(eq):
        t += step
        assert t < limit
        w.run(t)
    eq.set_state(State.FAULT)  # as FaultInjector does
    eq.fail_job("FAULT")
    w.run(t + 10)
    eq.set_state(State.IDLE)
    return eq


def test_asc_fault_while_holding_keeps_the_box_and_the_rerun_skips_the_pick(world2, rec):
    w, r = world2, rec(world2)
    w.racks.put("RK-A-1", "C1")
    job = Job("S1", JobType.STACK_IN, "C1", "RACK:RK-A-1", "SLOT:A:5:3")
    w.port.assign_job("ASC-A-WS", job)
    asc = _fault(w, "ASC-A-WS", lambda e: e.holding == "C1" and e.hoist_m > 5)
    assert r.failed("ASC-A-WS", "S1") == "FAULT"
    assert _where(w, "C1") == ["asc:ASC-A-WS"]
    assert w.port.snapshot("ASC-A-WS").extra["holding"] == "C1"
    w.racks.put("RK-A-2", "C2")  # no other pick while a box hangs in the spreader
    assert not w.port.assign_job("ASC-A-WS", Job("S2", JobType.STACK_IN, "C2", "RACK:RK-A-2", "SLOT:A:6:3"))
    assert w.port.assign_job("ASC-A-WS", Job("S3", JobType.STACK_IN, "C1", "RACK:RK-A-1", "SLOT:A:5:3"))
    w.run(w.now() + 1000)
    assert r.done("ASC-A-WS", "S3") is not None  # rack is empty: it did not pick again
    assert _where(w, "C1") == ["stack"] and asc.holding is None


def test_sts_load_fault_keeps_the_box_and_the_rerun_skips_the_pick(world2, rec):
    w, r = world2, rec(world2)
    agv = w.equipment["AGV-01"]
    agv.load("C9")
    w.port.assign_job("AGV-01", Job("T1", JobType.TRANSPORT, "C9", "BUFFER:BUF-01-1", "LANE:STS-01-L1"))
    load = Job("L1", JobType.LOAD, "C9", "LANE:STS-01-L1", "VESSEL:A:6:11:12")
    w.port.assign_job("STS-01", load)
    sts = _fault(w, "STS-01", lambda e: e.holding == "C9" and e.trolley_pos_m < 20)
    assert _where(w, "C9") == ["sts:STS-01"]
    assert w.port.snapshot("STS-01").extra["holding"] == "C9"
    assert not w.port.assign_job("STS-01", Job("D1", JobType.DISCHARGE, "C1", "VESSEL:A:6:0:12", "LANE:*"))
    assert w.port.assign_job("STS-01", Job("L2", JobType.LOAD, "C9", "LANE:STS-01-L1", "VESSEL:A:6:11:12"))
    w.run(w.now() + 300)
    assert r.done("STS-01", "L2") is not None
    assert _where(w, "C9") == ["vessel"] and sts.holding is None


def test_sts_discharge_fault_keeps_the_box(world2, rec):
    w, r = world2, rec(world2)
    w.port.assign_job("STS-01", Job("D1", JobType.DISCHARGE, "C1", "VESSEL:A:6:11:12", "LANE:STS-01-L1"))
    sts = _fault(w, "STS-01", lambda e: e.holding == "C1" and e.trolley_pos_m > 0)
    assert _where(w, "C1") == ["sts:STS-01"]
    w.port.assign_job("STS-01", Job("D2", JobType.DISCHARGE, "C1", "VESSEL:A:6:11:12", "LANE:STS-01-L1"))
    w.run(w.now() + 300)
    assert r.done("STS-01", "D2") is not None
    assert _where(w, "C1") == ["lane:STS-01-L1"] and sts.holding is None


def test_buffered_box_blocks_its_lane(world2, rec):
    w, r = world2, rec(world2)
    sts = w.equipment["STS-01"]
    w.port.assign_job("STS-01", Job("D1", JobType.DISCHARGE, "X", "VESSEL:A:6:11:12", "LANE:STS-01-L1"))
    w.run(150)
    assert sts.lane_buffer["STS-01-L1"] == "X"
    # an AGV for Y stands in the lane (it does not fetch X: its job is for Y)
    w.port.assign_job("AGV-01", Job("T1", JobType.TRANSPORT, "Y", "BUFFER:BUF-01-1", "LANE:STS-01-L1"))
    w.run(300)
    assert r.done("AGV-01", "T1") is not None and w.equipment["AGV-01"].loaded_container_id is None
    w.port.assign_job("STS-01", Job("D2", JobType.DISCHARGE, "Y", "VESSEL:A:6:11:12", "LANE:STS-01-L1"))
    w.run(900)
    assert r.done("STS-01", "D2") is None and sts.extra()["wait_for_agv_s"] > 0
    assert _where(w, "Y") == []  # still on the vessel: the crane did not pick it
    # the AGV in the lane fetches X; then Y may come down
    w.port.assign_job("AGV-01", Job("T2", JobType.TRANSPORT, "X", "LANE:STS-01-L1", "LANE:STS-01-L1"))
    w.run(1300)
    assert _where(w, "X") == ["agv:AGV-01"]
    assert r.done("STS-01", "D2") is not None
    assert _where(w, "Y") == ["lane:STS-01-L1"]


def test_sts_spreader_never_jumps_between_moves(world2):
    w = world2
    sts = w.equipment["STS-01"]
    jobs = [Job(f"D{i}", JobType.DISCHARGE, f"C{i}", f"VESSEL:A:6:{row}:{tier}", "LANE:*")
            for i, (row, tier) in enumerate([(0, 17), (21, 0), (5, 12)])]
    kin = sts.kin
    prev = (sts.trolley_pos_m, sts.hoist_pos_m)
    t = 0.0
    max_dh = max_dy = 0.0
    while t < 1500:
        if jobs and sts.state is State.IDLE:
            w.port.assign_job("STS-01", jobs.pop(0))
        t += 0.5
        w.run(t)
        cur = (sts.trolley_pos_m, sts.hoist_pos_m)
        max_dy = max(max_dy, abs(cur[0] - prev[0]))
        max_dh = max(max_dh, abs(cur[1] - prev[1]))
        prev = cur
    assert not jobs and sts.moves == 3
    assert max_dh <= kin.hoist_empty.v_max * 0.5 + 1e-6
    assert max_dy <= kin.trolley.v_max * 0.5 + 1e-6


@pytest.mark.parametrize("where_", ["agv", "lane_buffer", "stack"])
def test_reissued_discharge_for_a_box_already_ashore_is_refused(world2, rec, where_):
    """A DISCHARGE re-issued for a box that already left the vessel fails BOX_NOT_AT_SOURCE;
    the vessel never yields a second copy."""
    w, r = world2, rec(world2)
    if where_ == "agv":
        w.agvs["AGV-01"].load("C1")
    elif where_ == "lane_buffer":
        w.equipment["STS-01"].lane_buffer["STS-01-L2"] = "C1"
    else:
        w.stack.put("A", 3, 3, "C1")
    assert w.port.assign_job("STS-01", Job("D1", JobType.DISCHARGE, "C1", "VESSEL:A:6:11:12", "LANE:STS-01-L1"))
    w.run(300)
    assert r.failed("STS-01", "D1") == "BOX_NOT_AT_SOURCE"
    assert w.locate_box("C1") is not None
    assert w.equipment["STS-01"].lane_buffer["STS-01-L1"] is None and w.equipment["STS-01"].holding is None


def test_reissued_stack_in_for_a_stacked_box_is_refused(world2, rec):
    w, r = world2, rec(world2)
    w.stack.put("A", 3, 3, "C1")
    assert w.port.assign_job("ASC-A-WS", Job("S1", JobType.STACK_IN, "C1", "RACK:RK-A-1", "SLOT:A:5:3"))
    w.run(10)
    assert r.failed("ASC-A-WS", "S1") == "BOX_NOT_AT_SOURCE"
    assert w.stack.locate("C1") == ("A", 3, 3, 0)
