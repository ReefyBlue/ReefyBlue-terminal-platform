from __future__ import annotations

import random
from collections.abc import Callable

import salabim
from terminal_model.port import EquipmentEvent
from terminal_model.types import Job, JobType, Kind, Position, State

from terminal_sim.equipment.base import Equipment
from terminal_sim.grid import Cell, Grid

SPEED_EMPTY_MPS = 6.0
SPEED_LOADED_MPS = 4.0
BATTERY_PCT_PER_M = 0.02
ENERGY_KWH_PER_KM = 1.2
RESERVE_RETRY_S = 1.0
REPLAN_AFTER_S = 30.0
AUTO_CLEAR_AFTER_S = 5.0  # idle time on a handover cell before the AGV clears it


class AGV(Equipment):
    """Grid-routed transport vehicle (TRANSPORT jobs only).

    Auto-clear of handover cells: an AGV idle for ``AUTO_CLEAR_AFTER_S`` on a
    handover cell (STS lane cell or block TP cell) drives, BUSY but without job
    events, to the nearest free staging cell and goes IDLE again. Only an *empty*
    AGV with *no pending handover* does this, i.e. ``loaded_container_id`` and
    ``job_container_id`` are both None (the latter is cleared when the AGV unloads
    or its job fails / is cancelled). An empty AGV waiting under the crane for a
    discharge, or a loaded one waiting for a LOAD, keeps its lane cell.
    """

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
        handover_cells: frozenset[Cell] = frozenset(),
        staging_cells: frozenset[Cell] = frozenset(),
        on_arrival: Callable[[AGV], None] = lambda a: None,
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
        self.handover_cells = handover_cells  # STS lane / TP cells, cleared when idle
        self.staging_cells = staging_cells  # where an idle AGV parks
        # Called on arrival at the goal of a TRANSPORT, before JOB_COMPLETED (box handover).
        self.on_arrival = on_arrival
        self.cell: Cell = start_cell
        self.position = grid.center(start_cell)
        self.battery_pct = 100.0  # clamped at 0; no charging in Plan A
        self.battery_used_pct = 0.0  # cumulative drain, not clamped
        self.loaded_container_id: str | None = None
        # container the AGV was last sent for, until it hands that box over (or the job ends early)
        self.job_container_id: str | None = None
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
        self.job_container_id = None  # handover done
        self._wake_for_clear()
        return cid

    def _release_pending(self) -> None:
        self.grid.set_waiting(self.equipment_id, None)
        if self._pending is not None:
            self.grid.release(self._pending, self.equipment_id)
            self._pending = None

    def set_state(self, s: State) -> None:
        clearing = self.state is State.BUSY and self.current_job is None  # auto-clear move
        super().set_state(s)
        if s is State.FAULT and clearing:
            # Stop the clearing move where it is: drop the cell reserved ahead, so the
            # AGV holds exactly its own cell; it clears again once the fault is over.
            if self.env.current_component() is not self:
                self.cancel()
            self._release_pending()
            self.route = []
        self._wake_for_clear()

    def cancel_job(self) -> bool:
        self.job_container_id = None
        ok = super().cancel_job()
        self._release_pending()
        self.route = []
        return ok

    def fail_job(self, reason: str) -> None:
        self.job_container_id = None
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
            yield from self._step(nxt)
            if self.current_job is None:  # failed/cancelled meanwhile
                return
            i += 1
        self.route = []
        self.on_arrival(self)
        self.complete_job()

    def _step(self, nxt: Cell):
        """Drive into the reserved neighbour cell ``nxt``."""
        self._pending = nxt
        yield self.hold(self.grid.cell_m / self.speed_mps)
        self.grid.release(self.cell, self.equipment_id)
        self.cell = nxt
        self._pending = None
        self.position = self.grid.center(nxt)
        drain = BATTERY_PCT_PER_M * self.grid.cell_m
        self.battery_used_pct += drain
        self.battery_pct = max(0.0, self.battery_pct - drain)
        self.energy_kwh += ENERGY_KWH_PER_KM * self.grid.cell_m / 1000

    # --- auto-clear of handover cells ----------------------------------------
    def _clearable(self) -> bool:
        """Idle and empty on a handover cell with no handover pending: an AGV waiting
        empty for a discharge (or loaded for a load) keeps its lane cell."""
        return (
            self.state is State.IDLE
            and self.current_job is None
            and self.cell in self.handover_cells
            and self.loaded_container_id is None
            and self.job_container_id is None
            and bool(self.staging_cells)
        )

    def _wake_for_clear(self) -> None:
        # Became clearable from outside (reset, end of a fault, box taken by the crane):
        # start the driver. Inside the own process the _run loop handles it.
        if self._clearable() and self.env.current_component() is not self:
            self.activate(process="_run")

    def _idle_behaviour(self):
        return self._auto_clear() if self._clearable() else None

    def _staging_route(self) -> list[Cell]:
        others = self.grid.reserved_by_others(self.equipment_id)
        route = self.grid.path_to_nearest(self.cell, self.staging_cells, avoid=others)
        if route:
            return route
        # Boxed in: also route through cells of AGVs that wait for *our* cell. They
        # give way (``_give_way``) once this AGV waits for theirs; otherwise an AGV
        # arriving for the lane cell and the one leaving it block each other forever.
        yielding = {c for c in others if self.grid.waiting_for(self.grid.owner_of(c)) == self.cell}
        if not yielding:
            return []
        return self.grid.path_to_nearest(self.cell, self.staging_cells, avoid=others - yielding)

    def _auto_clear(self):
        """Idle on a handover cell for AUTO_CLEAR_AFTER_S: drive (BUSY, no job events)
        to the nearest free staging cell, then go IDLE. A job assigned meanwhile
        re-activates the driver and ends this."""
        while True:
            yield self.hold(AUTO_CLEAR_AFTER_S)
            if not self._clearable():
                return
            route = self._staging_route()
            if route:
                break
        self.set_state(State.BUSY)
        self.route = route
        i = 1
        while i < len(route):
            nxt = route[i]
            waited = 0.0
            while not self.grid.try_reserve(nxt, self.equipment_id):
                self.grid.set_waiting(self.equipment_id, nxt)
                if waited >= REPLAN_AFTER_S:
                    waited = 0.0
                    new = self._staging_route()
                    if new:
                        route, i, nxt = new, 1, new[1]
                        self.route = route
                        continue
                yield self.hold(RESERVE_RETRY_S)
                waited += RESERVE_RETRY_S
            self.grid.set_waiting(self.equipment_id, None)
            yield from self._step(nxt)
            if self.state is not State.BUSY:  # defensive: FAULT set from inside this process
                self.route = []
                return
            i += 1
        self.route = []
        self.set_state(State.IDLE)

    def extra(self) -> dict:
        return {
            "battery_pct": self.battery_pct,
            "battery_used_pct": self.battery_used_pct,
            "loaded_container_id": self.loaded_container_id,
            "route": [[c[0], c[1]] for c in self.route],
            "speed_mps": self.speed_mps,
        }
