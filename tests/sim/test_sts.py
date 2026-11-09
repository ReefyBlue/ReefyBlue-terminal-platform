import random

import pytest
import salabim

from terminal_model.port import EventKind
from terminal_model.types import Job, JobStatus, JobType, Kind, Position, State
from terminal_sim.equipment.sts import STS
from terminal_sim.grid import Grid


class Rig:
    def __init__(self):
        self.env = salabim.Environment(trace=False, yieldless=False)
        self.grid = Grid(20, 6, 20.0)
        self.events = []
        self.sts = STS(
            equipment_id="STS-01",
            kind=Kind.STS,
            position=Position(0.0, 0.0),
            rng=random.Random(1),
            emit=self.events.append,
            env=self.env,
            lane_cells=((4, 0), (5, 0)),
            grid=self.grid,
        )


@pytest.fixture
def rig():
    return Rig()


def job(job_type=JobType.DISCHARGE, job_id="J1"):
    return Job(job_id, job_type, "C1", "SHIP", "LANE")


def of_kind(rig, kind):
    return [e for e in rig.events if e.kind == kind]


def test_sts_rejects_wrong_job_type(rig):
    j = job(JobType.TRANSPORT)
    assert rig.sts.assign_job(j) is False
    assert rig.sts.state == State.IDLE
    assert rig.sts.current_job is None


def test_assign_rejected_unless_idle(rig):
    assert rig.sts.assign_job(job()) is True
    assert rig.sts.state == State.BUSY
    assert rig.sts.assign_job(job(job_id="J2")) is False


def test_sts_discharge_cycle_time_in_bounds(rig):
    rig.grid.try_reserve((4, 0), "AGV-01")
    j = job()
    assert rig.sts.assign_job(j)
    assert j.status == JobStatus.RUNNING
    rig.env.run(till=200)
    done = of_kind(rig, EventKind.JOB_COMPLETED)
    assert len(done) == 1
    assert 90 <= done[0].sim_time <= 150
    assert done[0].job_id == "J1"
    assert j.status == JobStatus.DONE
    assert rig.sts.state == State.IDLE
    assert rig.sts.energy_kwh == 5.0


def test_sts_accepts_second_job_after_completion(rig):
    rig.grid.try_reserve((5, 0), "AGV-01")  # second lane cell also counts
    rig.sts.assign_job(job())
    rig.env.run(till=200)
    assert rig.sts.assign_job(job(JobType.LOAD, "J2"))
    rig.env.run(till=400)
    assert len(of_kind(rig, EventKind.JOB_COMPLETED)) == 2
    assert rig.sts.energy_kwh == 10.0


def test_sts_waits_for_agv(rig):
    rig.sts.assign_job(job())
    rig.env.run(till=300)
    assert rig.sts.state == State.BUSY
    assert of_kind(rig, EventKind.JOB_COMPLETED) == []
    assert rig.sts.extra()["handover_lane_occupied"] is False


def test_sts_ignores_non_agv_owner(rig):
    rig.grid.try_reserve((4, 0), "ASC-01")
    rig.sts.assign_job(job())
    rig.env.run(till=300)
    assert rig.sts.state == State.BUSY


def test_sts_proceeds_when_agv_arrives_late(rig):
    rig.sts.assign_job(job())
    rig.env.run(till=100)
    rig.grid.try_reserve((5, 0), "AGV-02")
    rig.env.run(till=400)
    done = of_kind(rig, EventKind.JOB_COMPLETED)
    assert len(done) == 1
    assert done[0].sim_time >= 190


def test_cancel_then_reset(rig):
    j = job()
    rig.sts.assign_job(j)
    rig.env.run(till=50)
    assert rig.sts.cancel_job() is True
    failed = of_kind(rig, EventKind.JOB_FAILED)
    assert len(failed) == 1 and failed[0].reason == "CANCELLED"
    assert j.status == JobStatus.FAILED
    assert rig.sts.state == State.BUSY
    # cancelled process must not complete the job later
    rig.grid.try_reserve((4, 0), "AGV-01")
    rig.env.run(till=400)
    assert of_kind(rig, EventKind.JOB_COMPLETED) == []
    assert rig.sts.reset() is True
    assert rig.sts.state == State.IDLE


def test_cancel_without_job_is_false(rig):
    assert rig.sts.cancel_job() is False


def test_reset_from_fault_is_false(rig):
    rig.sts.set_state(State.FAULT)
    assert rig.sts.reset() is False
    assert rig.sts.state == State.FAULT


def test_set_state_emits_only_on_change(rig):
    rig.sts.set_state(State.IDLE)
    assert rig.events == []
    rig.sts.set_state(State.MAINTENANCE)
    assert len(rig.events) == 1
    assert rig.events[0].kind == EventKind.STATE_CHANGED
    assert rig.events[0].new_state == State.MAINTENANCE


def test_fail_job_emits_and_idles(rig):
    j = job()
    rig.sts.assign_job(j)
    rig.sts.fail_job("BREAKDOWN")
    assert j.status == JobStatus.FAILED
    assert of_kind(rig, EventKind.JOB_FAILED)[0].reason == "BREAKDOWN"
    assert rig.sts.state == State.IDLE
    assert rig.sts.current_job is None


def test_snapshot(rig):
    rig.grid.try_reserve((4, 0), "AGV-01")
    rig.sts.assign_job(job())
    s = rig.sts.snapshot()
    assert s.equipment_id == "STS-01"
    assert s.kind == Kind.STS
    assert s.vendor == "SIM"
    assert s.state == State.BUSY
    assert s.current_job_id == "J1"
    assert s.extra["berth_side"] == "WS"
    assert s.extra["handover_lane_occupied"] is True
    assert "spreader_height_m" in s.extra and "trolley_position_m" in s.extra
