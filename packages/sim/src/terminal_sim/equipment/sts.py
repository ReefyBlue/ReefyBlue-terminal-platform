from __future__ import annotations

import random
from collections.abc import Callable

import salabim

from terminal_model.port import EquipmentEvent
from terminal_model.types import Job, JobType, Kind, Position
from terminal_sim.equipment.base import Equipment
from terminal_sim.grid import Cell, Grid

AGV_POLL_S = 5
CYCLE_LOW_S, CYCLE_HIGH_S, CYCLE_MODE_S = 90, 150, 110
ENERGY_PER_MOVE_KWH = 5.0


class STS(Equipment):
    def __init__(
        self,
        *,
        equipment_id: str,
        kind: Kind,
        position: Position,
        rng: random.Random,
        emit: Callable[[EquipmentEvent], None],
        env: salabim.Environment,
        lane_cells: tuple[Cell, Cell],
        grid: Grid,
    ) -> None:
        super().__init__(
            equipment_id=equipment_id,
            kind=kind,
            position=position,
            rng=rng,
            emit=emit,
            env=env,
        )
        self.lane_cells = lane_cells
        self.grid = grid

    def accepts(self, job: Job) -> bool:
        return job.type in (JobType.DISCHARGE, JobType.LOAD)

    def agv_in_lane(self) -> bool:
        return any(
            (owner := self.grid.owner_of(c)) is not None and owner.startswith("AGV-")
            for c in self.lane_cells
        )

    def process(self):
        while not self.agv_in_lane():
            yield self.hold(AGV_POLL_S)
        yield self.hold(self.rng.triangular(CYCLE_LOW_S, CYCLE_HIGH_S, CYCLE_MODE_S))
        if self.current_job is None:  # failed/cancelled meanwhile
            return
        self.energy_kwh += ENERGY_PER_MOVE_KWH
        self.complete_job()

    def extra(self) -> dict:
        return {
            "berth_side": "WS",
            "spreader_height_m": 0.0,
            "trolley_position_m": 0.0,
            "handover_lane_occupied": self.agv_in_lane(),
        }
