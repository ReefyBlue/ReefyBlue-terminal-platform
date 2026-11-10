from __future__ import annotations

import random
from collections.abc import Callable

import salabim
from terminal_model.locations import SlotLoc, TransferPointLoc, parse_location
from terminal_model.port import EquipmentEvent
from terminal_model.types import Job, JobType, Kind, Position

from terminal_sim.equipment.base import Equipment
from terminal_sim.layout import BlockSpec
from terminal_sim.yard import Yard

BAY_PITCH_M = 6.5
ROW_PITCH_M = 3.0
TIER_H_M = 3.0
GANTRY_MPS = 4.0
TROLLEY_MPS = 1.5
HOIST_MPS = 1.0
ENERGY_PER_MOVE_KWH = 2.5

# A point is (bay, row, tier). The waterside transfer point sits at bay 0, row 1, tier 1.
_TP_POINT = (0.0, 1, 1)


def _leg_s(a: tuple[float, int, int], b: tuple[float, int, int]) -> float:
    return (
        abs(a[0] - b[0]) * BAY_PITCH_M / GANTRY_MPS
        + abs(a[1] - b[1]) * ROW_PITCH_M / TROLLEY_MPS
    )


def _hoist_s(tier: int) -> float:
    return 2 * tier * TIER_H_M / HOIST_MPS


def _parse(s: str):
    try:
        return parse_location(s)
    except ValueError:
        return None


class ASC(Equipment):
    def __init__(
        self,
        *,
        equipment_id: str,
        position: Position,
        rng: random.Random,
        emit: Callable[[EquipmentEvent], None],
        env: salabim.Environment,
        block: BlockSpec,
        yard: Yard,
    ) -> None:
        super().__init__(
            equipment_id=equipment_id,
            kind=Kind.ASC,
            position=position,
            rng=rng,
            emit=emit,
            env=env,
        )
        self.block = block
        self.yard = yard
        self.gantry_bay = 1.0
        # Waterside TP holds a box: from STACK_IN assignment (the AGV has put it down)
        # until that STACK_IN completes, and from STACK_OUT completion until the box is
        # taken by an AGV (``World.transfer_tp_to_agv``).
        self.tp_ws_occupied = False
        self.tp_ls_occupied = False

    def _is_ws_tp(self, loc) -> bool:
        return (
            isinstance(loc, TransferPointLoc)
            and loc.block == self.block.block
            and loc.side == "WS"
        )

    def _is_own_slot(self, loc) -> bool:
        return isinstance(loc, SlotLoc) and loc.block == self.block.block

    def accepts(self, job: Job) -> bool:
        src, dst = _parse(job.from_loc), _parse(job.to_loc)
        if job.type == JobType.STACK_IN:
            return self._is_ws_tp(src) and self._is_own_slot(dst)
        if job.type == JobType.STACK_OUT:
            return self._is_own_slot(src) and self._is_ws_tp(dst)
        return False

    def assign_job(self, job: Job) -> bool:
        ok = super().assign_job(job)
        if ok and job.type == JobType.STACK_IN:
            self.tp_ws_occupied = True
        return ok

    def process(self):
        job = self.current_job
        assert job is not None
        stack_in = job.type == JobType.STACK_IN
        slot = parse_location(job.to_loc if stack_in else job.from_loc)
        assert isinstance(slot, SlotLoc)

        if stack_in:
            try:
                free = self.yard.is_free(slot)
            except ValueError as e:
                self.fail_job(str(e))
                return
            if not free:
                self.fail_job("SLOT_OCCUPIED")
                return
        elif self.yard.locate(job.container_id) != slot:
            self.fail_job("SLOT_EMPTY")
            return

        slot_pt = (float(slot.bay), slot.row, slot.tier)
        pick, place = (_TP_POINT, slot_pt) if stack_in else (slot_pt, _TP_POINT)
        here = (self.gantry_bay, 1, 1)

        yield self.hold(_leg_s(here, pick))
        if self.current_job is None:
            return
        yield self.hold(_hoist_s(pick[2]))
        if self.current_job is None:
            return
        yield self.hold(_leg_s(pick, place))
        if self.current_job is None:
            return
        yield self.hold(_hoist_s(place[2]))
        if self.current_job is None:
            return

        try:
            if stack_in:
                self.yard.occupy(slot, job.container_id)
            else:
                self.yard.release(slot)
        except (ValueError, KeyError) as e:
            self.fail_job(str(e.args[0]) if e.args else "YARD_ERROR")
            return
        self.energy_kwh += ENERGY_PER_MOVE_KWH
        self.gantry_bay = float(slot.bay) if stack_in else 0.0
        self.tp_ws_occupied = not stack_in
        self.complete_job()

    def extra(self) -> dict:
        return {
            "block_id": self.block.block,
            "gantry_position_m": self.gantry_bay * BAY_PITCH_M,
            "ws_tp_occupied": self.tp_ws_occupied,
            "ls_tp_occupied": self.tp_ls_occupied,
        }
