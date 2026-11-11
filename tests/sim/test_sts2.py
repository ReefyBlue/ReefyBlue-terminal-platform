"""Sts2: vessel geometry, lanes with a lane buffer, gantry min-gap, passive AGV handover."""
from __future__ import annotations

import pytest
from terminal_model.layout import Berth
from terminal_model.types import Job, JobType
from terminal_sim.equipment.sts2 import Sts2
from terminal_sim.equipment.vessel import Vessel
from terminal_sim.kinematics import DEFAULTS

KIN = DEFAULTS["STS"]
BAY1 = 6  # x = 64.0, 2.67 m from STS-01


def _dis(jid, cid, bay=BAY1, row=11, tier=12, lane="STS-01-L1"):
    return Job(jid, JobType.DISCHARGE, cid, f"VESSEL:A:{bay}:{row}:{tier}", f"LANE:{lane}")


def _load(jid, cid, bay=BAY1, row=11, tier=12, lane="STS-01-L1"):
    return Job(jid, JobType.LOAD, cid, f"LANE:{lane}", f"VESSEL:A:{bay}:{row}:{tier}")


def _agv_to(jid, cid, to):
    return Job(jid, JobType.TRANSPORT, cid, "BUFFER:BUF-01-1", to)


def test_vessel_geometry_and_move_times():
    v = Vessel(Berth("A", 0.0, 400.0))
    assert v.bay_x(0) == 25.0 and v.bay_x(49) == 25.0 + 49 * 6.5
    assert v.tiers == 18
    fast = v.move_time(KIN, 0, 0, 10, laden=False)  # row 0, lowest deck tier
    slow = v.move_time(KIN, 0, 21, 0, laden=False)  # outer row, hold bottom
    mid = v.move_time(KIN, 0, 11, 12, laden=False)
    assert 55 < fast < mid < slow < 130
    assert 60 <= mid + KIN.twistlock_s <= 140
    # a LOAD move covers the same distances (laden in the other direction)
    assert v.move_time(KIN, 0, 11, 12, laden=True) == pytest.approx(mid, rel=0.1)
    with pytest.raises(ValueError):
        v.check(0, 22, 0)


def test_accepts_only_valid_vessel_and_lane(world2):
    sts = world2.equipment["STS-01"]
    assert isinstance(sts, Sts2)
    assert sts.accepts(_dis("J", "C1"))
    assert sts.accepts(_dis("J", "C1", lane="*"))
    assert not sts.accepts(_dis("J", "C1", lane="STS-02-L1"))  # other crane's lane
    assert not sts.accepts(_dis("J", "C1", row=22))
    assert not sts.accepts(Job("J", JobType.DISCHARGE, "C1", "STS-01", "LANE:STS-01-L1"))
    assert not sts.accepts(Job("J", JobType.TRANSPORT, "C1", "VESSEL:A:0:0:0", "LANE:STS-01-L1"))


def test_discharge_onto_waiting_agv(world2, rec):
    r = rec(world2)
    world2.port.assign_job("AGV-01", _agv_to("T1", "C1", "LANE:STS-01-L1"))
    world2.run(200)
    assert r.done("AGV-01", "T1") is not None
    assert world2.port.assign_job("STS-01", _dis("D1", "C1"))
    t0 = world2.now()
    world2.run(t0 + 300)
    took = r.done("STS-01", "D1") - t0
    assert 60 <= took <= 140
    snap = world2.port.snapshot("STS-01").extra
    assert snap["wait_for_agv_s"] == 0 and snap["lane_buffer_count"] == 0
    assert world2.port.snapshot("AGV-01").extra["loaded_container_id"] == "C1"


def test_discharge_without_agv_uses_buffer_then_waits(world2, rec):
    r = rec(world2)
    world2.port.assign_job("STS-01", _dis("D1", "C1"))
    world2.run(300)
    assert r.done("STS-01", "D1") is not None
    sts = world2.equipment["STS-01"]
    assert sts.lane_buffer["STS-01-L1"] == "C1"
    assert sts.extra()["wait_for_agv_s"] == 0
    # the same lane is full: the next discharge waits for an AGV
    world2.port.assign_job("STS-01", _dis("D2", "C2"))
    world2.run(900)
    assert r.done("STS-01", "D2") is None
    assert sts.extra()["wait_for_agv_s"] > 0
    # an AGV fetches C1 from the buffer -> the crane can set C2 down
    world2.port.assign_job("AGV-01", _agv_to("T1", "C1", "LANE:STS-01-L1"))
    world2.run(1500)
    assert r.done("AGV-01", "T1") is not None
    assert world2.equipment["AGV-01"].carries("C1")
    assert r.done("STS-01", "D2") is not None
    assert sts.lane_buffer["STS-01-L1"] == "C2"


def test_any_lane_picks_a_free_buffer(world2, rec):
    r = rec(world2)
    world2.port.assign_job("STS-01", _dis("D1", "C1", lane="*"))
    world2.run(300)
    world2.port.assign_job("STS-01", _dis("D2", "C2", lane="*"))
    world2.run(600)
    assert r.done("STS-01", "D2") is not None
    assert world2.equipment["STS-01"].lane_buffer == {
        "STS-01-L1": "C1", "STS-01-L2": "C2", "STS-01-L3": None}


def test_load_waits_for_the_box_then_takes_it_from_the_agv(world2, rec):
    r = rec(world2)
    world2.port.assign_job("STS-01", _load("L1", "C9"))
    world2.run(100)
    assert r.done("STS-01", "L1") is None
    agv = world2.equipment["AGV-01"]
    agv.load("C9")
    world2.port.assign_job("AGV-01", _agv_to("T1", "C9", "LANE:STS-01-L1"))
    world2.run(600)
    assert r.done("STS-01", "L1") is not None
    assert agv.loaded_container_id is None and agv.job_container_id is None
    assert world2.vessels["A"].loaded == ["C9"]
    assert world2.equipment["STS-01"].extra()["wait_for_agv_s"] > 0


def test_trolley_and_hoist_are_sampled_during_a_move(world2):
    sts = world2.equipment["STS-01"]
    world2.port.assign_job("STS-01", _dis("D1", "C1", row=21))
    seen = set()
    for t in range(1, 80):
        world2.run(t)
        seen.add(round(sts.trolley_pos_m, 1))
    assert len(seen) > 20
    assert min(seen) < -50  # the trolley went out over row 21


def test_gantry_keeps_min_gap_to_the_neighbour(world2, rec):
    r = rec(world2)
    s1, s2 = world2.equipment["STS-01"], world2.equipment["STS-02"]
    # bay 24 (x 181) is 19 m from STS-02 (x 200) < 27 m: STS-01 must wait
    world2.port.assign_job("STS-01", _dis("D1", "C1", bay=24))
    world2.run(100)
    assert s1.x_m == pytest.approx(66.667) and r.done("STS-01", "D1") is None
    # STS-02 moves east to bay 35 (x 252.5); then STS-01 may come
    world2.port.assign_job("STS-02", _dis("D2", "C2", bay=35, lane="STS-02-L1"))
    world2.run(1200)
    assert s2.x_m == pytest.approx(252.5)
    assert s1.x_m == pytest.approx(181.0)
    assert r.done("STS-01", "D1") is not None
