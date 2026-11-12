"""Per-order book-keeping for the dispatcher: stages, the order record and pure helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from terminal_model.locations import SlotLoc, parse_location
from terminal_model.types import Job, JobType

from terminal_ecs.workorders import WorkOrder

Column = tuple[str, int, int]  # rack mode: (block, bay, row), 0-based


class Stage(StrEnum):
    # discharge
    AWAIT_AGV = "AWAIT_AGV"
    AGV_EN_ROUTE = "AGV_EN_ROUTE"
    AT_STS = "AT_STS"
    DISCHARGING = "DISCHARGING"
    TO_YARD = "TO_YARD"
    AWAIT_TP = "AWAIT_TP"  # AGV with the box at the TP, another discharge box still on it
    STACKING = "STACKING"
    # load
    AWAIT_ASC = "AWAIT_ASC"
    UNSTACKING = "UNSTACKING"
    AT_TP = "AT_TP"
    AGV_TO_TP = "AGV_TO_TP"
    TO_STS = "TO_STS"
    AT_STS_LOAD = "AT_STS_LOAD"
    LOADING = "LOADING"
    # rack mode (v2 layouts with transfer zones; see ``Dispatcher`` "RACK MODE")
    TO_STS_LANE = "TO_STS_LANE"  # empty AGV driving to its STS lane
    AT_LANE = "AT_LANE"  # AGV in the lane; with a job: the STS DISCHARGE / LOAD runs
    LANE_PICKUP = "LANE_PICKUP"  # discharge box in the lane buffer; the AGV lifts it itself
    TO_RACK = "TO_RACK"  # discharge box on the AGV, driving to a transfer rack
    BUFFER_WAIT = "BUFFER_WAIT"  # discharge box on the AGV, no rack free: waiting in a buffer
    ON_RACK = "ON_RACK"  # box on the rack (discharge: for STACK_IN; load: for an AGV)
    AGV_TO_RACK = "AGV_TO_RACK"  # load: empty AGV driving to the rack to lift the box
    RACK_TO_STS = "RACK_TO_STS"  # load: box on the AGV, driving to the STS lane
    REHANDLE = "REHANDLE"  # LOAD / TRUCK_OUT: the LS-ASC moves a box off the order's box (STACK_MOVE)
    TRUCK = "TRUCK"  # TRUCK_IN / TRUCK_OUT: the LS-ASC's truck job runs
    # terminal
    DONE = "DONE"
    FAILED = "FAILED"


# Orders whose AGV is committed to the crane (heading for, waiting in or working in
# the STS lane; for a LOAD already from the moment it is sent to the TP); counted
# against ``lookahead_per_sts``.
LOOKAHEAD_STAGES = frozenset(
    {
        Stage.AGV_EN_ROUTE, Stage.AT_STS, Stage.DISCHARGING,  # discharge
        Stage.AGV_TO_TP, Stage.TO_STS, Stage.AT_STS_LOAD, Stage.LOADING,  # load
    }
)
# Stages in which the order's AGV stands in the STS lane (under or next to the crane).
IN_LANE = frozenset({Stage.AT_STS, Stage.AT_STS_LOAD})
# Orders that have not been served by their STS yet; the per-STS FIFO runs over these.
PRE_CRANE = frozenset(
    {
        Stage.AWAIT_AGV, Stage.AGV_EN_ROUTE, Stage.AT_STS,
        Stage.AWAIT_ASC, Stage.UNSTACKING, Stage.AT_TP, Stage.AGV_TO_TP, Stage.TO_STS, Stage.AT_STS_LOAD,
    }
)
# Stages that wait for an AGV to be sent (discharge: to the STS; load: to the TP).
NEEDS_AGV = frozenset({Stage.AWAIT_AGV, Stage.AT_TP})
# A FIFO-head LOAD in these stages will need an AGV; non-head orders keep one free for it.
HEAD_NEEDS_AGV_LATER = frozenset({Stage.AWAIT_ASC, Stage.UNSTACKING, Stage.AT_TP})
# The TP of a block holds one box: no new STACK_OUT while one of these exists for the block.
TP_BUSY = frozenset({Stage.UNSTACKING, Stage.AT_TP, Stage.AGV_TO_TP})
PENDING = frozenset({Stage.AWAIT_AGV, Stage.AWAIT_ASC})
# Stages in which the order's box is on its AGV: from the sts_to_agv / tp_to_agv
# hand-over until agv_to_tp (discharge, entering STACKING) / agv_to_sts (load, DONE).
# A failed LOADING rolls back to AT_STS_LOAD with the box still on board.
# Rack mode: TO_RACK, BUFFER_WAIT, RACK_TO_STS (and a LOAD in AT_LANE, see
# ``Dispatcher._rack_box_on_agv``; a DISCHARGE in AT_LANE has no box on its AGV yet).
BOX_ON_AGV = frozenset(
    {
        Stage.TO_YARD, Stage.AWAIT_TP, Stage.TO_STS, Stage.AT_STS_LOAD, Stage.LOADING,
        Stage.TO_RACK, Stage.BUFFER_WAIT, Stage.RACK_TO_STS,
    }
)

# Where an order goes when its running job fails. Stages missing here keep their stage
# (the box is on the AGV/at the TP) and simply get the job re-issued by ``tick()``.
ROLLBACK = {
    Stage.AGV_EN_ROUTE: Stage.AWAIT_AGV,
    Stage.DISCHARGING: Stage.AT_STS,
    Stage.UNSTACKING: Stage.AWAIT_ASC,
    Stage.AGV_TO_TP: Stage.AT_TP,
    Stage.LOADING: Stage.AT_STS_LOAD,
    Stage.TO_STS_LANE: Stage.AWAIT_AGV,  # rack mode
    Stage.AGV_TO_RACK: Stage.ON_RACK,  # rack mode
}
# Rollbacks where the AGV carries no box yet and is released.
RELEASE_AGV = frozenset({Stage.AGV_EN_ROUTE, Stage.AGV_TO_TP})
# Rack mode: a failed STACK_IN goes back to ON_RACK (the box is on the rack or, after a
# fault, in the ASC's spreader: the same job is re-issued to the same crane). Stages kept:
# AT_LANE (STS job; a held box is set down by the re-issued job), LANE_PICKUP, TO_RACK,
# BUFFER_WAIT, RACK_TO_STS (box on the AGV or in the lane buffer).
# A failed STACK_MOVE / truck job goes back to AWAIT_ASC (re-planned; a box held in the
# LS-ASC's spreader is set down by the re-issued job).
RACK_ROLLBACK = {**ROLLBACK, Stage.STACKING: Stage.ON_RACK, Stage.REHANDLE: Stage.AWAIT_ASC,
                 Stage.TRUCK: Stage.AWAIT_ASC}
RACK_RELEASE_AGV = frozenset({Stage.TO_STS_LANE, Stage.AGV_TO_RACK})


@dataclass(frozen=True)
class LoggedJob:
    """A job the dispatcher got accepted: by whom and when (kept after it ends)."""

    job: Job
    equipment_id: str
    assigned_at: float


@dataclass(frozen=True)
class ContainerView:
    """One order as ``GET /containers`` lists it; ``seq`` is the number of its last
    change between open / done / failed (a cursor for ``Dispatcher.containers_since``)."""

    seq: int
    container_id: str
    state: Literal["open", "done", "failed"]
    job_type: Literal["DISCHARGE", "LOAD", "TRUCK_IN", "TRUCK_OUT"]
    yard_slot: str


@dataclass
class OrderRecord:
    order: WorkOrder
    yard_slot: str
    stage: Stage = Stage.AWAIT_AGV
    agv_id: str | None = None
    current_job_id: str | None = None
    seq: int = 0  # set on every stage entry; FIFO key for waiting orders
    submit_seq: int = 0  # position in the TOS sequence; restored as ``seq`` on rollback
    fail_reason: str | None = None
    agv_at: str | None = None  # where a bound AGV stands when not at its STS (re-slotted at a TP)
    watchdog_cancels: int = 0  # reset when one of the order's jobs completes
    stuck: bool = False  # watchdog gave up re-cancelling (see Dispatcher._watchdog)
    replan_streak: int = 0  # JOB_FAILED/FAULT replans in a row (cap -> REPLAN_STREAK); reset on completion
    job_counts: dict[JobType, int] = field(default_factory=dict)
    change_seq: int = 0  # bumped when taken, DONE or FAILED (``containers_since``); not a FIFO key
    # rack mode only
    vessel: str | None = None  # VESSEL:<berth>:<bay>:<row>:<tier> of the STS move
    lane: str | None = None  # STS lane held for the order's AGV (and its buffered box)
    rack: str | None = None  # transfer rack claimed for the box
    buffer: str | None = None  # apron buffer of a BUFFER_WAIT AGV
    avoid_racks: set[str] = field(default_factory=set)  # refused with RACK_OCCUPIED; re-picked
    rehandles: int = 0  # STACK_MOVEs completed to dig this order's box out
    move: tuple[str, Column, Column] | None = None  # (box, from, to) of its running / held STACK_MOVE
    dig_col: Column | None = None  # column being dug (no box set down on it meanwhile)
    move_retries: int = 0  # STACK_MOVE target re-picked after COLUMN_FULL-style failures
    avoid_cols: set[Column] = field(default_factory=set)  # STACK_MOVE targets found full
    truck_lane: int | None = None  # truck lane of a TRUCK_IN / TRUCK_OUT (kept for a re-issue)

    @property
    def cid(self) -> str:
        return self.order.container_id

    @property
    def block(self) -> str:
        return self.yard_slot.split("/", 1)[0]

    @property
    def tp(self) -> str:
        return f"TP/{self.block}/WS"

    def next_job_id(self, job_type: JobType) -> str:
        return f"{self.cid}-{job_type}-{self.job_counts.get(job_type, 0) + 1}"


def valid_slot(slot: str, block: str) -> bool:
    """True if ``slot`` parses as a yard slot in ``block``."""
    try:
        loc = parse_location(slot)
    except ValueError:
        return False
    return isinstance(loc, SlotLoc) and loc.block == block
