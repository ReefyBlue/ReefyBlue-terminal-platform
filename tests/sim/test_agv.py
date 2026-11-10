import random

import pytest
import salabim
from terminal_model.port import EventKind
from terminal_model.types import Job, JobType, Position, State
from terminal_sim.equipment.agv import AGV
from terminal_sim.grid import Grid

LOCS = {"STS-01": (4, 0), "TP/A/WS": (2, 5)}


class Rig:
    def __init__(self, grid=None, locs=None):
        self.env = salabim.Environment(trace=False, yieldless=False)
        self.grid = grid or Grid(20, 6, 20.0)
        self.events = []
        self.locs = locs or LOCS

    def agv(self, n, start):
        return AGV(
            equipment_id=f"AGV-{n:02d}",
            position=Position(0.0, 0.0),
            rng=random.Random(n),
            emit=self.events.append,
            env=self.env,
            start_cell=start,
            grid=self.grid,
            resolve_cell=self.locs.__getitem__,
        )

    def owned_by(self, owner):
        return [
            (x, y)
            for x in range(self.grid.cols)
            for y in range(self.grid.rows)
            if self.grid.owner_of((x, y)) == owner
        ]


@pytest.fixture
def rig():
    return Rig()


def transport(to_loc, job_id="J1"):
    return Job(job_id, JobType.TRANSPORT, "C1", "X", to_loc)


def of_kind(rig, kind):
    return [e for e in rig.events if e.kind == kind]


def test_speed_depends_on_load(rig):
    a = rig.agv(1, (0, 2))
    assert a.speed_mps == 6.0
    a.load("C1")
    assert a.speed_mps == 4.0
    with pytest.raises(ValueError):
        a.load("C2")
    assert a.unload() == "C1"
    assert a.speed_mps == 6.0
    with pytest.raises(ValueError):
        a.unload()


def test_start_cell_reserved_and_occupied_start_rejected(rig):
    rig.agv(1, (0, 2))
    assert rig.grid.owner_of((0, 2)) == "AGV-01"
    with pytest.raises(ValueError, match="START_CELL_OCCUPIED"):
        rig.agv(2, (0, 2))


def test_rejects_non_transport(rig):
    a = rig.agv(1, (0, 2))
    assert a.assign_job(Job("J1", JobType.LOAD, "C1", "X", "STS-01")) is False


def test_transport_arrives_and_holds_cell(rig):
    a = rig.agv(1, (0, 2))
    assert a.assign_job(transport("STS-01"))
    rig.env.run(till=100)
    assert len(of_kind(rig, EventKind.JOB_COMPLETED)) == 1
    assert a.state == State.IDLE
    assert a.cell == (4, 0)
    assert rig.grid.owner_of((4, 0)) == "AGV-01"
    assert rig.owned_by("AGV-01") == [(4, 0)]
    assert a.battery_pct < 100.0
    assert a.energy_kwh > 0
    assert a.extra()["route"] == []


def test_two_agvs_never_share_cell(rig):
    a = rig.agv(1, (0, 2))
    b = rig.agv(2, (3, 0))
    rig.locs["E"] = (8, 2)
    rig.locs["S"] = (3, 4)
    assert a.assign_job(transport("E", "J1"))
    assert b.assign_job(transport("S", "J2"))
    steps = 0
    while len(of_kind(rig, EventKind.JOB_COMPLETED)) < 2:
        rig.env.step()
        steps += 1
        assert steps < 1000, "did not finish"
        assert a.cell != b.cell
        owners = [
            rig.grid.owner_of((x, y))
            for x in range(rig.grid.cols)
            for y in range(rig.grid.rows)
        ]
        owned = [o for o in owners if o is not None]
        assert len(owned) <= 4  # at most two cells per AGV
    assert a.cell == (8, 2) and b.cell == (3, 4)
    assert rig.owned_by("AGV-01") == [(8, 2)]
    assert rig.owned_by("AGV-02") == [(3, 4)]


def test_cancel_mid_route_keeps_cell_reserved(rig):
    a = rig.agv(1, (0, 2))
    assert a.assign_job(transport("STS-01"))
    rig.env.run(till=7)  # mid-hold on the third cell
    assert a.cell != (0, 2)
    assert a.cancel_job() is True
    rig.env.run(till=100)
    assert a.cell != (4, 0)
    assert rig.owned_by("AGV-01") == [a.cell]
    assert a.current_job is None
    assert of_kind(rig, EventKind.JOB_FAILED)[0].reason == "CANCELLED"


def test_unreachable_goal_fails():
    rig = Rig(grid=Grid(2, 1, 20.0), locs={"W": (0, 0)})
    a = rig.agv(1, (1, 0))
    assert a.assign_job(transport("W"))
    rig.env.run(till=10)
    failed = of_kind(rig, EventKind.JOB_FAILED)
    assert len(failed) == 1 and failed[0].reason == "NO_ROUTE"
    assert a.state == State.IDLE
    assert rig.owned_by("AGV-01") == [(1, 0)]
