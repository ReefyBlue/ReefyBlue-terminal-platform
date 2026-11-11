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
