import random

import pytest
import salabim
from terminal_model.port import EventKind
from terminal_model.types import Job, JobType, Position, State
from terminal_sim.equipment.agv import AGV, AUTO_CLEAR_AFTER_S
from terminal_sim.grid import Grid

LOCS = {"STS-01": (4, 0), "TP/A/WS": (2, 5)}


class Rig:
    def __init__(self, grid=None, locs=None):
        self.env = salabim.Environment(trace=False, yieldless=False)
        self.grid = grid or Grid(20, 6, 20.0)
        self.events = []
        self.locs = locs or LOCS

    def agv(self, n, start, *, handover=frozenset(), staging=frozenset()):
        return AGV(
            equipment_id=f"AGV-{n:02d}",
            position=Position(0.0, 0.0),
            rng=random.Random(n),
            emit=self.events.append,
            env=self.env,
            start_cell=start,
            grid=self.grid,
            resolve_cell=self.locs.__getitem__,
            handover_cells=handover,
            staging_cells=staging,
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
    extra = a.snapshot().extra
    assert (extra["last_job_id"], extra["last_job_outcome"]) == (of_kind(rig, EventKind.JOB_FAILED)[0].job_id, "FAILED")


def test_unreachable_goal_fails():
    rig = Rig(grid=Grid(2, 1, 20.0), locs={"W": (0, 0)})
    a = rig.agv(1, (1, 0))
    assert a.assign_job(transport("W"))
    rig.env.run(till=10)
    failed = of_kind(rig, EventKind.JOB_FAILED)
    assert len(failed) == 1 and failed[0].reason == "NO_ROUTE"
    assert a.state == State.IDLE
    assert rig.owned_by("AGV-01") == [(1, 0)]


def test_plans_around_parked_agv():
    rig = Rig()
    rig.agv(2, (1, 2))
    a = rig.agv(1, (0, 2))
    assert a.assign_job(transport("TP/A/WS"))
    rig.env.run(till=100)
    assert a.cell == (2, 5)
    assert len(of_kind(rig, EventKind.JOB_COMPLETED)) == 1


def test_replans_when_blocked_mid_route():
    rig = Rig(locs={"X": (8, 2)})
    a = rig.agv(1, (0, 2))
    assert a.assign_job(transport("X"))
    rig.env.run(till=5)
    assert a.cell == (1, 2)
    rig.agv(2, (4, 2))  # parks ahead on the planned route
    rig.env.run(till=25)
    assert a.cell == (3, 2) and a.state == State.BUSY  # still waiting, < 30 s
    rig.env.run(till=200)
    assert a.cell == (8, 2)
    assert len(of_kind(rig, EventKind.JOB_COMPLETED)) == 1



class _FreeLaneCell:
    """resolve_cell for an STS: the first lane cell no AGV holds (like World)."""

    def __init__(self, grid, lane):
        self.grid, self.lane = grid, lane

    def __getitem__(self, loc):
        free = [c for c in self.lane if self.grid.owner_of(c) is None]
        return free[0] if free else self.lane[0]


def test_blocked_goal_is_re_resolved():
    # Both AGVs resolve the STS to the same free lane cell; the one that loses
    # the race must take the other lane cell instead of waiting forever.
    rig = Rig()
    lane = [(4, 0), (5, 0)]
    rig.locs = _FreeLaneCell(rig.grid, lane)
    a, b = rig.agv(1, (0, 2)), rig.agv(2, (1, 2))
    assert a.assign_job(transport("STS-01", "J1"))
    assert b.assign_job(transport("STS-01", "J2"))
    rig.env.run(till=300)
    assert len(of_kind(rig, EventKind.JOB_COMPLETED)) == 2
    assert {a.cell, b.cell} == set(lane)


def test_head_on_deadlock_is_broken_by_giving_way():
    # AGV-01 leaves the lane cell (4, 0) via (4, 1); AGV-02 sits on (4, 1) waiting
    # for (4, 0). The other exit (5, 0) is held. Without giving way both wait forever.
    rig = Rig(locs={"LANE": (4, 0), "TP/A/WS": (2, 5)})
    rig.grid.try_reserve((5, 0), "X")
    a, b = rig.agv(1, (4, 0)), rig.agv(2, (4, 1))
    assert a.assign_job(transport("TP/A/WS", "J1"))
    assert b.assign_job(transport("LANE", "J2"))
    rig.env.run(till=300)
    assert len(of_kind(rig, EventKind.JOB_COMPLETED)) == 2
    assert (a.cell, b.cell) == ((2, 5), (4, 0))
    assert rig.owned_by("AGV-01") == [(2, 5)] and rig.owned_by("AGV-02") == [(4, 0)]


HANDOVER = frozenset({(4, 0), (5, 0), (2, 5)})
STAGING = frozenset((x, y) for x in range(20) for y in (2, 3)) - HANDOVER


def parking_rig():
    rig = Rig()
    return rig, rig.agv(1, (0, 2), handover=HANDOVER, staging=STAGING)


def run_until_completed(rig, n=1):
    while len(of_kind(rig, EventKind.JOB_COMPLETED)) < n:
        assert rig.env.now() < 1000, "job did not complete"
        rig.env.run(till=rig.env.now() + 0.5)
    return of_kind(rig, EventKind.JOB_COMPLETED)[n - 1].sim_time


def states(rig):
    return [e.new_state for e in of_kind(rig, EventKind.STATE_CHANGED)]


def test_idle_agv_clears_handover_cell():
    rig, a = parking_rig()
    a.load("C1")
    assert a.assign_job(transport("TP/A/WS"))
    arrived = run_until_completed(rig)
    assert a.cell == (2, 5) and a.state == State.IDLE  # arrived at the TP
    assert a.unload() == "C1"  # box handed to the TP (the on_transfer hook does this)
    rig.env.run(till=arrived + AUTO_CLEAR_AFTER_S - 0.5)
    assert a.cell == (2, 5) and a.state == State.IDLE  # grace period
    rig.env.run(till=arrived + AUTO_CLEAR_AFTER_S + 0.5)
    assert a.state == State.BUSY and a.current_job is None  # clearing, no job
    assert a.route
    battery = a.battery_pct
    rig.env.run(till=arrived + 200)
    assert a.state == State.IDLE
    assert a.cell in STAGING and a.cell[1] in (2, 3)
    assert rig.grid.owner_of((2, 5)) is None
    assert rig.owned_by("AGV-01") == [a.cell]
    assert a.battery_pct < battery  # the clearing move is accounted
    assert states(rig) == [State.BUSY, State.IDLE, State.BUSY, State.IDLE]
    assert len(of_kind(rig, EventKind.JOB_COMPLETED)) == 1  # no job events for the move
    assert of_kind(rig, EventKind.JOB_FAILED) == []


def test_agv_with_new_job_within_grace_does_not_clear():
    rig, a = parking_rig()
    rig.locs["E"] = (8, 4)
    a.load("C1")
    assert a.assign_job(transport("TP/A/WS"))
    arrived = run_until_completed(rig)
    a.unload()
    rig.env.run(till=arrived + AUTO_CLEAR_AFTER_S - 1)
    assert a.cell == (2, 5) and a.state == State.IDLE
    assert a.assign_job(transport("E", "J2"))
    rig.env.run(till=arrived + 200)
    assert a.cell == (8, 4) and a.state == State.IDLE
    assert [e.job_id for e in of_kind(rig, EventKind.JOB_COMPLETED)] == ["J1", "J2"]
    assert states(rig) == [State.BUSY, State.IDLE, State.BUSY, State.IDLE]


def test_idle_agv_off_handover_cells_never_moves():
    rig, a = parking_rig()
    rig.locs["E"] = (8, 4)
    assert a.assign_job(transport("E"))
    rig.env.run(till=1000)
    assert a.cell == (8, 4) and a.state == State.IDLE
    assert states(rig) == [State.BUSY, State.IDLE]


def test_agv_waiting_for_a_handover_keeps_the_lane_cell():
    # Empty AGV sent for a discharge waits under the crane; a loaded one waits for
    # the crane to take its box. Neither may leave the lane.
    rig, a = parking_rig()
    assert a.assign_job(transport("STS-01"))  # sent for C1, nothing on board
    run_until_completed(rig)
    rig.env.run(till=500)
    assert a.cell == (4, 0) and a.state == State.IDLE
    a.load("C1")  # loaded and idle in the lane, as while waiting for a LOAD
    rig.env.run(till=1000)
    assert a.cell == (4, 0)
    assert a.unload() == "C1"  # LOAD done: the crane took the box
    rig.env.run(till=1000 + AUTO_CLEAR_AFTER_S + 100)
    assert a.cell in STAGING and a.state == State.IDLE
    assert rig.grid.owner_of((4, 0)) is None


def test_released_agv_clears_after_cancel_and_reset():
    # Watchdog path: the job is cancelled and the AGV reset while it stands in the lane.
    rig = Rig(locs={"FAR": (19, 0)})
    a = rig.agv(1, (4, 0), handover=HANDOVER, staging=STAGING)
    assert a.assign_job(transport("FAR"))
    rig.env.run(till=1)  # still on (4, 0), driving towards (5, 0)
    assert a.cancel_job() and a.job_container_id is None
    assert a.reset()
    assert a.cell == (4, 0)
    rig.env.run(till=200)
    assert a.cell in STAGING and a.state == State.IDLE


def test_auto_clear_waits_when_no_staging_cell_is_reachable():
    rig = Rig(grid=Grid(3, 1, 20.0), locs={"T": (2, 0)})
    a = rig.agv(1, (0, 0), handover=frozenset({(2, 0)}), staging=frozenset({(1, 0)}))
    a.load("C1")
    assert a.assign_job(transport("T"))
    run_until_completed(rig)
    a.unload()
    rig.env.run(till=100)
    assert a.cell == (2, 0) and a.state == State.IDLE  # (1, 0) is behind a one-way lane
    assert states(rig) == [State.BUSY, State.IDLE]


def test_boxed_in_agv_clears_through_an_agv_waiting_for_its_cell():
    # The empty AGV-01 on lane cell (4, 0) can only leave via (5, 0) (held by a loaded
    # AGV waiting for its LOAD) or (4, 1), where AGV-02 waits for (4, 0). AGV-01 must
    # claim (4, 1) so that AGV-02 gives way; then AGV-02 reaches the lane cell.
    rig = Rig(locs={"STS-01": (4, 0)})
    a = rig.agv(1, (4, 0), handover=HANDOVER, staging=STAGING)
    rig.grid.try_reserve((5, 0), "AGV-09")  # parked loaded AGV in the other lane cell
    b = rig.agv(2, (4, 1), handover=HANDOVER, staging=STAGING)
    b.load("C2")
    assert b.assign_job(transport("STS-01", "J2"))
    a.load("C1")
    a.unload()  # the crane took AGV-01's box: it starts clearing the lane cell
    rig.env.run(till=600)
    assert [e.job_id for e in of_kind(rig, EventKind.JOB_COMPLETED)] == ["J2"]
    assert b.cell == (4, 0)
    assert a.cell in STAGING and a.state == State.IDLE
    assert rig.owned_by("AGV-01") == [a.cell] and rig.owned_by("AGV-02") == [(4, 0)]


def test_fault_during_auto_clear_releases_the_cell_reserved_ahead():
    # A short fault (here: shorter than one step) hits an AGV while it clears a TP.
    # The clearing move must stop; the cell reserved ahead must be released.
    rig, a = parking_rig()
    a.load("C1")
    assert a.assign_job(transport("TP/A/WS"))
    arrived = run_until_completed(rig)
    a.unload()
    t = arrived + AUTO_CLEAR_AFTER_S
    while a._pending is None:  # clearing, mid-step with the next cell reserved
        t += 0.1
        rig.env.run(till=t)
        assert t < arrived + 60
    assert a.state == State.BUSY and a.current_job is None
    a.set_state(State.FAULT)
    assert a.route == [] and a._pending is None
    assert rig.owned_by("AGV-01") == [a.cell]
    rig.env.run(till=t + 0.5)
    a.set_state(State.IDLE)  # repaired: clearing starts over from the current cell
    assert rig.owned_by("AGV-01") == [a.cell]
    rig.env.run(till=t + 200)
    assert a.state == State.IDLE and a.cell in STAGING
    assert rig.owned_by("AGV-01") == [a.cell]
    assert rig.grid.waiting_for("AGV-01") is None


def test_battery_is_clamped_at_zero_but_drain_keeps_counting(rig):
    rig.locs["FAR"] = (19, 0)
    a = rig.agv(1, (0, 0))
    a.battery_pct = 0.1
    assert a.assign_job(transport("FAR"))
    rig.env.run(till=300)
    assert a.cell == (19, 0)
    assert a.battery_pct == 0.0
    assert a.extra()["battery_used_pct"] == pytest.approx(19 * 20.0 * 0.02)
