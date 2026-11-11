"""LiftAGV: kinematic per-cell driving, exclusive lane/rack points, rack handling, slots."""
from __future__ import annotations

import json

import pytest
from terminal_model.types import Job, JobType
from terminal_sim.equipment.lift_agv import LiftAGV
from terminal_sim.kinematics import DEFAULTS

KIN = DEFAULTS["AGV"]
RACK_OP_S = KIN.rack_position_s + KIN.rack_lift_s


def _t(jid, cid, to, frm="BUFFER:BUF-01-1"):
    return Job(jid, JobType.TRANSPORT, cid, frm, to)


def test_cell_times_straight_turning_zone(make_world2, rec):
    w = make_world2(n_agv=1)
    r = rec(w)
    agv = w.equipment["AGV-01"]
    assert agv.cell == (2, 4)  # BUF-01-1
    # (2,4) -> (3,4): east on an even row, heading unchanged -> straight
    w.port.assign_job("AGV-01", _t("T1", "", "BUFFER:BUF-01-2"))
    w.run(100)
    assert r.done("AGV-01", "T1") == pytest.approx(KIN.straight.travel_time(20.0))
    # (3,4) -> (3,3) turning north, -> (3,2) lane cell (zone), then the leg to the lane point
    t0 = w.now()
    w.port.assign_job("AGV-01", _t("T2", "", "LANE:STS-01-L1", frm="BUFFER:BUF-01-2"))
    w.run(t0 + 200)
    leg = ((70.0 - 63.167) ** 2 + (53.0 - 50.0) ** 2) ** 0.5
    expect = (KIN.turning.travel_time(20.0) + KIN.zone.travel_time(20.0)
              + KIN.zone.travel_time(leg))
    assert r.done("AGV-01", "T2") - t0 == pytest.approx(expect, abs=1e-6)
    assert agv.heading_deg == 270.0 and agv.at_point == "STS-01-L1"
    assert agv.position.x == pytest.approx(63.167) and agv.position.y == pytest.approx(53.0)


def test_position_is_interpolated_within_a_cell(make_world2):
    w = make_world2(n_agv=1)
    agv = w.equipment["AGV-01"]
    w.port.assign_job("AGV-01", _t("T1", "", "BUFFER:BUF-01-2"))
    w.run(4.0)
    assert 50.0 < agv.position.x < 70.0 and agv.position.y == 90.0
    assert agv.speed_mps > 0
    w.run(100)
    assert agv.position.x == 70.0 and agv.speed_mps == 0.0


def test_lane_point_is_exclusive(make_world2, rec):
    w = make_world2(n_agv=2)
    r = rec(w)
    w.port.assign_job("AGV-01", _t("T1", "C1", "LANE:STS-01-L1"))
    w.port.assign_job("AGV-02", _t("T2", "C2", "LANE:STS-01-L1"))
    w.run(400)
    done = [r.done("AGV-01", "T1"), r.done("AGV-02", "T2")]
    assert sum(d is not None for d in done) == 1
    first = "AGV-01" if done[0] is not None else "AGV-02"
    assert w.equipment[first].at_point == "STS-01-L1"
    assert w.points["STS-01-L1"] == first
    # the first leaves for its buffer; the second gets the lane
    w.port.assign_job(first, _t("T3", "", "BUFFER:BUF-01-3"))
    w.run(800)
    other = "AGV-02" if first == "AGV-01" else "AGV-01"
    assert w.equipment[other].at_point == "STS-01-L1"


def test_rack_put_and_pick_take_position_plus_lift(make_world2, rec):
    w = make_world2(n_agv=1)
    r = rec(w)
    agv = w.equipment["AGV-01"]
    agv.load("C1")
    w.port.assign_job("AGV-01", _t("T1", "C1", "RACK:RK-A-1"))
    arrived = None
    t = 0.0
    while r.done("AGV-01", "T1") is None and t < 1000:
        t += 0.5
        w.run(t)
        if arrived is None and agv.at_point == "RK-A-1":
            arrived = t
    assert r.done("AGV-01", "T1") - arrived == pytest.approx(RACK_OP_S, abs=0.5)
    assert w.racks.occupant("RK-A-1") == "C1" and agv.loaded_container_id is None
    # pick it up again
    w.port.assign_job("AGV-01", _t("T2", "C1", "RACK:RK-A-1", frm="RACK:RK-A-1"))
    t0 = w.now()
    w.run(t0 + 100)
    assert r.done("AGV-01", "T2") - t0 == pytest.approx(RACK_OP_S)
    assert w.racks.occupant("RK-A-1") is None and agv.loaded_container_id == "C1"


def test_rack_occupied_and_rack_empty_fail(make_world2, rec):
    w = make_world2(n_agv=1)
    r = rec(w)
    agv = w.equipment["AGV-01"]
    w.racks.put("RK-A-1", "X")
    agv.load("C1")
    w.port.assign_job("AGV-01", _t("T1", "C1", "RACK:RK-A-1"))
    w.run(1000)
    assert r.failed("AGV-01", "T1") == "RACK_OCCUPIED"
    assert agv.loaded_container_id == "C1"  # the box stays on board
    w.port.assign_job("AGV-01", _t("T2", "C7", "RACK:RK-A-2"))
    w.run(1500)
    # C1 on board but the job is for C7, not on the rack -> RACK_EMPTY
    assert r.failed("AGV-01", "T2") == "RACK_EMPTY"


def test_bad_target_fails(world2, rec):
    r = rec(world2)
    world2.port.assign_job("AGV-01", _t("T1", "C1", "RACK:NOPE"))
    world2.run(1)
    assert r.failed("AGV-01", "T1") == "BAD_LOCATION"


def test_slots_and_twin_20(make_world2):
    w = make_world2(n_agv=1)
    agv = w.equipment["AGV-01"]
    assert isinstance(agv, LiftAGV)
    agv.load("C1")
    with pytest.raises(ValueError):
        agv.load("C2")  # single box without twin_20
    w2 = make_world2(n_agv=1, twin_20=True)
    a2 = w2.equipment["AGV-01"]
    assert a2.load("C1") == "A" and a2.load("C2") == "B"
    ex = a2.extra()
    assert (ex["load_a"], ex["load_b"], ex["loaded_container_id"]) == ("C1", "C2", "C1")
    assert a2.unload("C2") == "C2" and a2.slots == {"A": "C1", "B": None}
    json.dumps(ex)
    assert {"heading_deg", "speed_mps", "charge_pct", "route"} <= set(ex)


def test_battery_drains_while_driving(make_world2):
    w = make_world2(n_agv=1)
    agv = w.equipment["AGV-01"]
    w.port.assign_job("AGV-01", _t("T1", "", "LANE:STS-01-L1"))
    w.run(500)
    assert agv.charge_pct < 100.0 and agv.energy_kwh > 0
