from __future__ import annotations

import logging
import random
from collections.abc import Callable
from typing import NamedTuple

import salabim
from terminal_model.port import EquipmentEvent, EquipmentSnapshot, EventKind
from terminal_model.types import Job, JobType, Kind

from terminal_sim.equipment.agv import AGV
from terminal_sim.equipment.asc import ASC
from terminal_sim.equipment.base import Equipment
from terminal_sim.equipment.sts import STS
from terminal_sim.faults import FaultInjector
from terminal_sim.grid import Cell, Grid
from terminal_sim.layout import BlockSpec, Layout
from terminal_sim.scenario import Scenario
from terminal_sim.yard import Yard

STAGING_ROWS = (2, 3)  # AGV-area interior between quay lanes and yard TPs

log = logging.getLogger("terminal_sim.world")


class SimEquipmentPort:
    """Thin EquipmentPort adapter over ``World.equipment``."""

    def __init__(self, world: World) -> None:
        self._world = world
        self._subscribers: list[Callable[[EquipmentEvent], None]] = []

    def _get(self, equipment_id: str) -> Equipment:
        return self._world.equipment[equipment_id]  # KeyError for unknown id

    def _dispatch(self, event: EquipmentEvent) -> None:
        for cb in list(self._subscribers):
            cb(event)

    def equipment_ids(self) -> list[str]:
        return list(self._world.equipment)

    def snapshot(self, equipment_id: str) -> EquipmentSnapshot:
        return self._get(equipment_id).snapshot()

    def assign_job(self, equipment_id: str, job: Job) -> bool:
        return self._get(equipment_id).assign_job(job)

    def cancel_job(self, equipment_id: str) -> bool:
        return self._get(equipment_id).cancel_job()

    def reset(self, equipment_id: str) -> bool:
        return self._get(equipment_id).reset()

    def subscribe(self, callback: Callable[[EquipmentEvent], None]) -> None:
        self._subscribers.append(callback)

    def now(self) -> float:
        return self._world.now()


DISCHARGE_BOX = "discharge"  # put on the TP by an AGV, waiting for STACK_IN
LOAD_BOX = "stack_out"  # put on the TP by the ASC, waiting for an AGV


class TpBox(NamedTuple):
    cid: str
    origin: str  # DISCHARGE_BOX or LOAD_BOX


class Handover:
    """Box handover between STS, AGV and ASC, done by the sim itself (no ECS hooks).

    - STS DISCHARGE ends: the box goes on the empty lane AGV sent for it
      (``job_container_id``); no such AGV -> the STS fails the job ``NO_AGV_IN_LANE``
      (``try_discharge``, the STS ``on_discharge_done`` callback).
    - AGV arrives on a block's TP cell (``on_arrival``, before its JOB_COMPLETED):
      loaded -> the box goes on the TP, unless the TP already holds a discharge box:
      then the AGV keeps it on board and stays (loaded, so it does not auto-clear);
      empty and its ``job_container_id`` is on the TP -> the AGV takes it.
    - ASC STACK_OUT completes -> LOAD box on the TP. STACK_IN completes -> box off the
      TP, then a loaded idle AGV waiting on that TP cell puts its box down.
    - STS LOAD completes -> the lane AGV carrying that box is emptied.

    This mirrors the ECS TP model (one discharge box per TP; a LOAD box may share).
    ``tp_box[block]`` holds the boxes on the waterside TP in arrival order, tagged with
    their origin; the ASC's ``tp_ws_occupied`` is True while it is non-empty.
    ``on_event`` must run before any other subscriber (``World`` subscribes it first)
    so the box has moved when the ECS sees the event (e.g. AWAIT_TP -> delivered in
    the tick after a STACK_IN completes).
    """

    def __init__(self, world: World) -> None:
        self.world = world
        self.tp_box: dict[str, list[TpBox]] = {b.block: [] for b in world.layout.blocks}
        self._block_at: dict[Cell, BlockSpec] = {b.tp_ws_cell: b for b in world.layout.blocks}

    def boxes(self, block: str) -> list[str]:
        """Container ids on ``block``'s waterside TP, in arrival order."""
        return [b.cid for b in self.tp_box[block]]

    def _sync(self, block: BlockSpec) -> None:
        asc = self.world.equipment.get(block.asc_id)
        if isinstance(asc, ASC):
            asc.tp_ws_occupied = bool(self.tp_box[block.block])

    def _has_discharge_box(self, block: BlockSpec) -> bool:
        return any(b.origin == DISCHARGE_BOX for b in self.tp_box[block.block])

    def put_on_tp(self, cell: Cell, cid: str, origin: str = DISCHARGE_BOX) -> bool:
        """Record ``cid`` on the TP at ``cell``; False if ``cell`` is no TP."""
        block = self._block_at.get(cell)
        if block is None:
            return False
        self.tp_box[block.block].append(TpBox(cid, origin))
        self._sync(block)
        return True

    def take_from_tp(self, cell: Cell, cid: str) -> bool:
        """Remove ``cid`` from the TP at ``cell``; False if it is not there."""
        block = self._block_at.get(cell)
        if block is None:
            return False
        boxes = self.tp_box[block.block]
        for i, b in enumerate(boxes):
            if b.cid == cid:
                del boxes[i]
                self._sync(block)
                return True
        return False

    def _lane_agv(self, sts: STS, match: Callable[[AGV], bool]) -> AGV | None:
        for eq in self.world.equipment.values():
            if isinstance(eq, AGV) and eq.cell in sts.lane_cells and match(eq):
                return eq
        return None

    def try_discharge(self, sts: STS) -> bool:
        assert sts.current_job is not None
        cid = sts.current_job.container_id
        agv = self._lane_agv(
            sts, lambda a: a.loaded_container_id is None and a.job_container_id == cid
        )
        if agv is None:
            return False
        agv.load(cid)
        return True

    def on_arrival(self, agv: AGV) -> None:
        block = self._block_at.get(agv.cell)
        if block is None:
            return
        if agv.loaded_container_id is not None:
            if self._has_discharge_box(block):
                return  # TP full: keep the box on board until the STACK_IN completes
            self.put_on_tp(agv.cell, agv.unload())
        elif agv.job_container_id is not None and self.take_from_tp(agv.cell, agv.job_container_id):
            agv.load(agv.job_container_id)

    def _release_waiting_agv(self, block: BlockSpec) -> None:
        if self._has_discharge_box(block):
            return
        for eq in self.world.equipment.values():
            if (
                isinstance(eq, AGV)
                and eq.cell == block.tp_ws_cell
                and eq.loaded_container_id is not None
                and eq.current_job is None
            ):
                self.put_on_tp(eq.cell, eq.unload())
                return

    def on_event(self, ev: EquipmentEvent) -> None:
        if ev.kind is not EventKind.JOB_COMPLETED:
            return
        eq = self.world.equipment.get(ev.equipment_id)
        job = eq.current_job if eq is not None else None
        if job is None or job.job_id != ev.job_id:
            return
        if isinstance(eq, ASC):
            cell = eq.block.tp_ws_cell
            if job.type is JobType.STACK_OUT:
                self.put_on_tp(cell, job.container_id, LOAD_BOX)
            elif job.type is JobType.STACK_IN:
                self.take_from_tp(cell, job.container_id)
                self._release_waiting_agv(eq.block)
            self._sync(eq.block)
        elif isinstance(eq, STS) and job.type is JobType.LOAD:
            cid = job.container_id
            agv = self._lane_agv(eq, lambda a: a.loaded_container_id == cid)
            if agv is None:
                log.error("LOAD %s on %s: no lane AGV carries the box", cid, eq.equipment_id)
                return
            agv.unload()


class World:
    def __init__(self, layout: Layout, scenario: Scenario) -> None:
        self.layout = layout
        self.scenario = scenario
        self.env = salabim.Environment(
            trace=False, yieldless=False, random_seed=scenario.seed
        )
        self.rng = random.Random(scenario.seed)
        self.grid = Grid(layout.grid_cols, layout.grid_rows, layout.grid_cell_m)
        self.yard = Yard(layout.blocks)
        self.port = SimEquipmentPort(self)
        self.equipment: dict[str, Equipment] = {}
        self.handover = Handover(self)
        self.port.subscribe(self.handover.on_event)  # first: boxes move before the ECS sees events
        emit = self.port._dispatch
        # AGVs clear handover cells (STS lanes, TPs) when idle and park in rows 2-3.
        self.handover_cells = frozenset(
            [c for s in layout.sts for c in s.lane_cells] + [b.tp_ws_cell for b in layout.blocks]
        )
        self.staging_cells = frozenset(
            (x, y) for x in range(layout.grid_cols) for y in STAGING_ROWS if y < layout.grid_rows
        ) - self.handover_cells

        for spec in layout.sts[: scenario.n_sts]:
            self.equipment[spec.equipment_id] = STS(
                equipment_id=spec.equipment_id,
                kind=Kind.STS,
                position=spec.position,
                rng=self.rng,
                emit=emit,
                env=self.env,
                lane_cells=spec.lane_cells,
                grid=self.grid,
                on_discharge_done=self.handover.try_discharge,
            )
        for i, cell in enumerate(layout.agv_start_cells[: scenario.n_agv], start=1):
            agv_id = f"AGV-{i:02d}"
            self.equipment[agv_id] = AGV(
                equipment_id=agv_id,
                position=self.grid.center(cell),
                rng=self.rng,
                emit=emit,
                env=self.env,
                start_cell=cell,
                grid=self.grid,
                resolve_cell=self.resolve_cell,
                handover_cells=self.handover_cells,
                staging_cells=self.staging_cells,
                on_arrival=self.handover.on_arrival,
            )
        for block in layout.blocks:
            self.equipment[block.asc_id] = ASC(
                equipment_id=block.asc_id,
                position=block.tp_ws,
                rng=self.rng,
                emit=emit,
                env=self.env,
                block=block,
                yard=self.yard,
            )

        self.fault_injectors: list[FaultInjector] = []
        for eq in self.equipment.values():
            params = scenario.faults.get(eq.kind)
            if params is not None and params.mtbf_s > 0:
                self.fault_injectors.append(
                    FaultInjector(target=eq, params=params, rng=self.rng, env=self.env)
                )

    def faults_total(self) -> int:
        return sum(i.count for i in self.fault_injectors)

    def run(self, until: float) -> None:
        self.env.run(till=until)

    def now(self) -> float:
        return self.env.now()

    def resolve_cell(self, loc: str) -> Cell:
        if loc.startswith("STS-"):
            spec = next((s for s in self.layout.sts if s.equipment_id == loc), None)
            if spec is None:
                raise ValueError(f"unknown location {loc!r}")
            for cell in spec.lane_cells:
                owner = self.grid.owner_of(cell)
                if owner is None or not owner.startswith("AGV-"):
                    return cell
            return spec.lane_cells[0]
        parts = loc.split("/")
        if len(parts) == 3 and parts[0] == "TP" and parts[2] == "WS":
            for block in self.layout.blocks:
                if block.block == parts[1]:
                    return block.tp_ws_cell
        raise ValueError(f"unknown location {loc!r}")

    # Manual handovers (tests / tools); the runner leaves handover to ``Handover``.
    def transfer_sts_to_agv(self, sts_id: str, container_id: str) -> str | None:
        """Put ``container_id`` on the empty lane AGV that was sent for it (None if absent)."""
        spec = next((s for s in self.layout.sts if s.equipment_id == sts_id), None)
        if spec is None:
            raise KeyError(sts_id)
        for eq in self.equipment.values():
            if (
                isinstance(eq, AGV)
                and eq.cell in spec.lane_cells
                and eq.loaded_container_id is None
                and eq.job_container_id == container_id
            ):
                eq.load(container_id)
                return eq.equipment_id
        return None

    def transfer_tp_to_agv(self, agv_id: str, container_id: str) -> None:
        """The AGV standing on a block's TP cell takes ``container_id`` from that TP."""
        agv = self.equipment[agv_id]
        if not isinstance(agv, AGV):
            raise TypeError(f"{agv_id} is not an AGV")
        agv.load(container_id)
        if not self.handover.take_from_tp(agv.cell, container_id):
            for eq in self.equipment.values():  # box not recorded: still free the TP flag
                if isinstance(eq, ASC) and eq.block.tp_ws_cell == agv.cell:
                    eq.tp_ws_occupied = bool(self.handover.tp_box[eq.block.block])

    def transfer_agv_to_tp(self, agv_id: str) -> str:
        agv = self.equipment[agv_id]
        assert isinstance(agv, AGV)
        cid = agv.unload()
        self.handover.put_on_tp(agv.cell, cid)
        return cid


def make_world(layout: Layout, scenario: Scenario):
    """``World2`` for a v2 layout (``layout.terminal`` set), else the legacy ``World``."""
    if layout.terminal is not None:
        from terminal_sim.world2 import World2  # world2 imports this module

        return World2(layout, scenario)
    return World(layout, scenario)
