"""Lift-AGV of the v2 world: drives the derived grid with kinematic per-cell times, carries one
box per slot (A, B; B only with ``twin_20``) and lifts boxes on/off transfer racks itself.

Job: ``TRANSPORT`` with ``to_loc`` ``LANE:<id>`` | ``RACK:<id>`` | ``BUFFER:<id>`` (``from_loc`` is
where the leg starts; informational). The AGV drives to the target cell (legacy ``Grid`` router
and cell reservations; lane and rack cells are shared zone cells, the lane/rack *point* inside
is exclusive), then:

- ``RACK``: carrying the job's box -> set it on the rack (``rack_position_s + rack_lift_s``;
  ``RACK_OCCUPIED`` fails the job, the box stays on board); empty and the box is on the rack ->
  lift it off; otherwise ``RACK_EMPTY``. The AGV never waits for an ASC.
- ``LANE``: passive - the STS sets the box on / takes it from the AGV. If the job's box waits in
  the lane buffer, the AGV lifts it itself (same rack times).
- ``BUFFER``: park.

Per-cell time: ``Axis.travel_time(cell_m)`` of the ``straight`` axis, ``turning`` when the heading
changes into that cell, ``zone`` into lane/transfer cells and for the leg to the point.
The position is interpolated every ``SAMPLE_DT``.
"""
from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterator

import salabim
from terminal_model.port import EquipmentEvent
from terminal_model.types import Job, JobType, Kind, Position

from terminal_sim.equipment.base import Equipment
from terminal_sim.equipment.motion import sampled_hold
from terminal_sim.grid import Cell, Grid
from terminal_sim.kinematics import AgvKin, Axis

RESERVE_RETRY_S = 1.0
REPLAN_AFTER_S = 30.0
BATTERY_PCT_PER_M = 0.02
BATTERY_PCT_PER_LIFT = 0.05
ENERGY_KWH_PER_KM = 1.2
ENERGY_KWH_PER_LIFT = 0.05
SLOTS = ("A", "B")


class LiftAGV(Equipment):
    def __init__(
        self, *, equipment_id: str, rng: random.Random, emit: Callable[[EquipmentEvent], None],
        env: salabim.Environment, start_cell: Cell, grid: Grid, kin: AgvKin, hub,
        twin_20: bool = False,
    ) -> None:
        super().__init__(equipment_id=equipment_id, kind=Kind.AGV, position=grid.center(start_cell),
                         rng=rng, emit=emit, env=env)
        if not grid.try_reserve(start_cell, equipment_id):
            raise ValueError("START_CELL_OCCUPIED")
        self.grid = grid
        self.kin = kin
        self.hub = hub
        self.twin_20 = twin_20
        self.cell: Cell = start_cell
        self.heading_deg = 0.0
        self.speed_mps = 0.0
        self.slots: dict[str, str | None] = {s: None for s in SLOTS}
        self.charge_pct = 100.0
        self.route: list[Cell] = []
        self.at_point: str | None = None  # lane/rack id the AGV stands at
        self.point_res: str | None = None  # lane/rack id reserved by this AGV
        self.job_container_id: str | None = None
        self._pending: Cell | None = None

    # --- load ---------------------------------------------------------------
    @property
    def loaded_container_id(self) -> str | None:
        return self.slots["A"] or self.slots["B"]

    def carries(self, cid: str) -> bool:
        return cid in self.slots.values()

    def free_slot(self) -> str | None:
        if self.slots["A"] is None:
            return "A" if self.slots["B"] is None or self.twin_20 else None
        if self.twin_20 and self.slots["B"] is None:
            return "B"
        return None

    def load(self, cid: str) -> str:
        slot = self.free_slot()
        if slot is None:
            raise ValueError("AGV has no free slot")
        self.slots[slot] = cid
        return slot

    def unload(self, cid: str | None = None) -> str:
        for s in SLOTS:
            if self.slots[s] is not None and (cid is None or self.slots[s] == cid):
                out, self.slots[s] = self.slots[s], None
                if out == self.job_container_id:
                    self.job_container_id = None  # handover done
                return out
        raise ValueError(f"AGV does not carry {cid or 'a box'}")

    # --- jobs -----------------------------------------------------------------
    def accepts(self, job: Job) -> bool:
        return job.type is JobType.TRANSPORT

    def assign_job(self, job: Job) -> bool:
        ok = super().assign_job(job)
        if ok:
            self.job_container_id = job.container_id
        return ok

    def _abort(self) -> None:
        self.hub.release_racks(self.equipment_id)
        self.grid.set_waiting(self.equipment_id, None)
        if self._pending is not None:
            self.grid.release(self._pending, self.equipment_id)
            self._pending = None
        self.route = []
        self.speed_mps = 0.0

    def cancel_job(self) -> bool:
        self.job_container_id = None
        ok = super().cancel_job()
        self._abort()
        return ok

    def fail_job(self, reason: str) -> None:
        self.job_container_id = None
        super().fail_job(reason)
        self._abort()

    # --- routing (legacy Grid router) ---------------------------------------------
    def _plan(self, goal: Cell) -> list[Cell]:
        route = self.grid.shortest_path(
            self.cell, goal, avoid=self.grid.reserved_by_others(self.equipment_id))
        return route or self.grid.shortest_path(self.cell, goal)

    def _give_way(self, blocked: Cell, goal: Cell) -> list[Cell]:
        blocker = self.grid.owner_of(blocked)
        if blocker is None or self.grid.waiting_for(blocker) != self.cell:
            return []
        for side in self.grid.neighbors(self.cell):
            if side == blocked or self.grid.owner_of(side) is not None:
                continue
            onward = (self.grid.shortest_path(side, goal, avoid={blocked})
                      or self.grid.shortest_path(side, goal))
            if onward:
                return [self.cell, *onward]
        return []

    def _axis_into(self, nxt: Cell) -> tuple[Axis, float]:
        dx, dy = nxt[0] - self.cell[0], nxt[1] - self.cell[1]
        heading = math.degrees(math.atan2(dy, dx)) % 360
        if nxt in self.hub.zone_cells:
            axis = self.kin.zone
        elif abs(heading - self.heading_deg) > 1e-6:
            axis = self.kin.turning
        else:
            axis = self.kin.straight
        return axis, heading

    def _drive(self, axis: Axis, dist: float, target: Position) -> Iterator:
        start = self.position
        last = [0.0, 0.0]  # (t, s)

        def upd(t: float) -> None:
            s = axis.position_at(dist, t)
            f = s / dist if dist > 0 else 1.0
            self.position = Position(start.x + f * (target.x - start.x), start.y + f * (target.y - start.y))
            dt = t - last[0]
            self.speed_mps = (s - last[1]) / dt if dt > 0 else 0.0
            last[:] = [t, s]

        yield from sampled_hold(self, axis.travel_time(dist), upd)
        self.position = target
        self.speed_mps = 0.0
        self._drain(dist)

    def _drain(self, dist: float) -> None:
        self.charge_pct = max(0.0, self.charge_pct - BATTERY_PCT_PER_M * dist)
        self.energy_kwh += ENERGY_KWH_PER_KM * dist / 1000

    def _step(self, nxt: Cell) -> Iterator:
        self._pending = nxt
        axis, heading = self._axis_into(nxt)
        self.heading_deg = heading
        yield from self._drive(axis, self.grid.cell_m, self.grid.center(nxt))
        self.grid.release(self.cell, self.equipment_id)
        self.cell = nxt
        self._pending = None

    def _leave_point(self, keep: str | None) -> None:
        if self.point_res is not None and self.point_res != keep:
            self.hub.release_point(self.point_res, self.equipment_id)
            self.point_res = None
        if self.at_point != keep:
            self.at_point = None

    def _lift(self) -> Iterator:
        yield self.hold(self.kin.rack_position_s + self.kin.rack_lift_s)
        self.charge_pct = max(0.0, self.charge_pct - BATTERY_PCT_PER_LIFT)
        self.energy_kwh += ENERGY_KWH_PER_LIFT

    def process(self):
        job = self.current_job
        assert job is not None
        try:
            goal, pid, pxy, kind = self.hub.resolve_agv(job.to_loc)
        except ValueError:
            self.fail_job("BAD_LOCATION")
            return
        self._leave_point(pid)
        if pid is None or self.at_point != pid:
            route = self._plan(goal)
            if not route:
                self.fail_job("NO_ROUTE")
                return
            self.route = route
            i = 1
            while i < len(route):
                nxt = route[i]
                waited = 0.0
                while not self.grid.try_reserve(nxt, self.equipment_id):
                    self.grid.set_waiting(self.equipment_id, nxt)
                    if waited >= REPLAN_AFTER_S:
                        waited = 0.0
                        new = self._plan(goal) if nxt != goal else []
                        if len(new) < 2 or new[1] == nxt:
                            new = self._give_way(nxt, goal) or new
                        if len(new) > 1:
                            route, i, nxt = new, 1, new[1]
                            self.route = route
                            continue
                    yield self.hold(RESERVE_RETRY_S)
                    waited += RESERVE_RETRY_S
                self.grid.set_waiting(self.equipment_id, None)
                yield from self._step(nxt)
                i += 1
            self.route = []
            if pid is not None:
                while not self.hub.reserve_point(pid, self.equipment_id):
                    yield self.hold(RESERVE_RETRY_S)
                self.point_res = pid
                if kind == "RACK" and self.carries(job.container_id) and not self.hub.reserve_rack(
                        pid, self.equipment_id):
                    self.fail_job("RACK_OCCUPIED")  # before the final approach; box stays on board
                    return
                dist = self.position.distance_to(pxy)
                yield from self._drive(self.kin.zone, dist, pxy)
                self.at_point = pid
        cid = job.container_id
        if kind == "RACK":
            if self.carries(cid):
                if not self.hub.reserve_rack(pid, self.equipment_id):
                    self.fail_job("RACK_OCCUPIED")
                    return
                yield from self._lift()
                self.hub.transfer_agv_to_rack(self, pid, cid)  # booked: cannot be taken meanwhile
            elif self.free_slot() is not None and self.hub.rack_occupant(pid) == cid:
                yield from self._lift()
                if self.hub.rack_occupant(pid) != cid:  # an ASC lifted it meanwhile
                    self.fail_job("RACK_EMPTY")
                    return
                self.hub.transfer_rack_to_agv(self, pid, cid)
            else:
                self.fail_job("RACK_EMPTY")
                return
        elif (kind == "LANE" and not self.carries(cid) and self.free_slot() is not None
              and self.hub.lane_buffer_of(pid) == cid):
            yield from self._lift()
            self.hub.transfer_lane_to_agv(self, pid, cid)
        self.complete_job()

    def extra(self) -> dict:
        return {
            "battery_pct": self.charge_pct,
            "charge_pct": self.charge_pct,
            "loaded_container_id": self.loaded_container_id,
            "load_a": self.slots["A"],
            "load_b": self.slots["B"],
            "route": [[c[0], c[1]] for c in self.route],
            "speed_mps": self.speed_mps,
            "heading_deg": self.heading_deg,
            "at_point": self.at_point,
        }

