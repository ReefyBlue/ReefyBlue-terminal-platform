from __future__ import annotations

import random
from collections.abc import Callable

import salabim
from terminal_model.port import EquipmentEvent, EquipmentSnapshot
from terminal_model.types import Job, Kind

from terminal_sim.equipment.agv import AGV
from terminal_sim.equipment.asc import ASC
from terminal_sim.equipment.base import Equipment
from terminal_sim.equipment.sts import STS
from terminal_sim.faults import FaultInjector
from terminal_sim.grid import Cell, Grid
from terminal_sim.layout import Layout
from terminal_sim.scenario import Scenario
from terminal_sim.yard import Yard

STAGING_ROWS = (2, 3)  # AGV-area interior between quay lanes and yard TPs


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

    def transfer_agv_to_tp(self, agv_id: str) -> str:
        agv = self.equipment[agv_id]
        assert isinstance(agv, AGV)
        return agv.unload()
