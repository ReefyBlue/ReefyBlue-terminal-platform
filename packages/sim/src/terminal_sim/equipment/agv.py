from __future__ import annotations

import random
from collections.abc import Callable

import salabim
from terminal_model.port import EquipmentEvent
from terminal_model.types import Job, JobType, Kind, Position

from terminal_sim.equipment.base import Equipment
from terminal_sim.grid import Cell, Grid

SPEED_EMPTY_MPS = 6.0
SPEED_LOADED_MPS = 4.0
BATTERY_PCT_PER_M = 0.02
ENERGY_KWH_PER_KM = 1.2
RESERVE_RETRY_S = 1.0
REPLAN_AFTER_S = 30.0


class AGV(Equipment):
    def __init__(
        self,
        *,
        equipment_id: str,
        position: Position,
        rng: random.Random,
        emit: Callable[[EquipmentEvent], None],
        env: salabim.Environment,
        start_cell: Cell,
        grid: Grid,
        resolve_cell: Callable[[str], Cell],
    ) -> None:
        super().__init__(
            equipment_id=equipment_id,
            kind=Kind.AGV,
            position=position,
            rng=rng,
            emit=emit,
            env=env,
        )
        if not grid.try_reserve(start_cell, equipment_id):
            raise ValueError("START_CELL_OCCUPIED")
        self.grid = grid
        self.resolve_cell = resolve_cell
        self.cell: Cell = start_cell
        self.position = grid.center(start_cell)
        self.battery_pct = 100.0
        self.loaded_container_id: str | None = None
        self.job_container_id: str | None = None  # container of the last accepted job
        self.route: list[Cell] = []
        self._pending: Cell | None = None  # cell reserved ahead during a move

    @property
    def speed_mps(self) -> float:
        return SPEED_EMPTY_MPS if self.loaded_container_id is None else SPEED_LOADED_MPS

    def accepts(self, job: Job) -> bool:
        return job.type == JobType.TRANSPORT

    def assign_job(self, job: Job) -> bool:
        ok = super().assign_job(job)
        if ok:
            self.job_container_id = job.container_id
        return ok

    def load(self, container_id: str) -> None:
        if self.loaded_container_id is not None:
            raise ValueError("AGV already loaded")
        self.loaded_container_id = container_id

    def unload(self) -> str:
        if self.loaded_container_id is None:
            raise ValueError("AGV not loaded")
        cid, self.loaded_container_id = self.loaded_container_id, None
        return cid

    def _release_pending(self) -> None:
        self.grid.set_waiting(self.equipment_id, None)
        if self._pending is not None:
            self.grid.release(self._pending, self.equipment_id)
            self._pending = None

    def cancel_job(self) -> bool:
        ok = super().cancel_job()
        self._release_pending()
        self.route = []
        return ok

    def fail_job(self, reason: str) -> None:
        super().fail_job(reason)
        self._release_pending()
        self.route = []

    def _plan(self, goal: Cell) -> list[Cell]:
        route = self.grid.shortest_path(
            self.cell, goal, avoid=self.grid.reserved_by_others(self.equipment_id)
        )
        return route or self.grid.shortest_path(self.cell, goal)

    def _give_way(self, blocked: Cell, goal: Cell) -> list[Cell]:
        """Break a head-on deadlock: if the AGV holding ``blocked`` waits for our
        cell, step aside to a free neighbour and route on to ``goal`` from there."""
        blocker = self.grid.owner_of(blocked)
        if blocker is None or self.grid.waiting_for(blocker) != self.cell:
            return []
        for side in self.grid.neighbors(self.cell):
            if side == blocked or self.grid.owner_of(side) is not None:
                continue
            onward = self.grid.shortest_path(side, goal, avoid={blocked}) or self.grid.shortest_path(side, goal)
            if onward:
                return [self.cell, *onward]
        return []

    def process(self):
        job = self.current_job
        assert job is not None
        goal = self.resolve_cell(job.to_loc)
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
                    # Re-resolve the goal: another AGV may hold the cell this one was
                    # heading for (e.g. the other STS lane cell is still free).
                    new_goal = self.resolve_cell(job.to_loc)
                    new: list[Cell] = []
                    if nxt != goal or new_goal != goal:
                        goal = new_goal
                        new = self._plan(goal)
                    if len(new) < 2 or new[1] == nxt:  # no detour: maybe a head-on deadlock
                        new = self._give_way(nxt, goal) or new
                    if len(new) > 1:
                        route, i, nxt = new, 1, new[1]
                        self.route = route
                        continue
                yield self.hold(RESERVE_RETRY_S)
                waited += RESERVE_RETRY_S
            self.grid.set_waiting(self.equipment_id, None)
            self._pending = nxt
            yield self.hold(self.grid.cell_m / self.speed_mps)
            prev = self.cell
            self.grid.release(prev, self.equipment_id)
            self.cell = nxt
            self._pending = None
            self.position = self.grid.center(nxt)
            self.battery_pct -= BATTERY_PCT_PER_M * self.grid.cell_m
            self.energy_kwh += ENERGY_KWH_PER_KM * self.grid.cell_m / 1000
            if self.current_job is None:  # failed/cancelled meanwhile
                return
            i += 1
        self.route = []
        self.complete_job()

    def extra(self) -> dict:
        return {
            "battery_pct": self.battery_pct,
            "loaded_container_id": self.loaded_container_id,
            "route": [[c[0], c[1]] for c in self.route],
            "speed_mps": self.speed_mps,
        }
