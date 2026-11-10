"""Per-order book-keeping for the dispatcher: stages, the order record and pure helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from terminal_model.locations import SlotLoc, parse_location
from terminal_model.types import JobType

from terminal_ecs.workorders import WorkOrder


class Stage(StrEnum):
    # discharge
    AWAIT_AGV = "AWAIT_AGV"
    AGV_EN_ROUTE = "AGV_EN_ROUTE"
    AT_STS = "AT_STS"
    DISCHARGING = "DISCHARGING"
    TO_YARD = "TO_YARD"
    STACKING = "STACKING"
    # load
    AWAIT_ASC = "AWAIT_ASC"
    UNSTACKING = "UNSTACKING"
    AT_TP = "AT_TP"
    AGV_TO_TP = "AGV_TO_TP"
    TO_STS = "TO_STS"
    AT_STS_LOAD = "AT_STS_LOAD"
    LOADING = "LOADING"
    # terminal
    DONE = "DONE"
    FAILED = "FAILED"


# Orders whose AGV is bound to an STS lane; counted against ``lookahead_per_sts``.
LANE_STAGES = frozenset(
    {Stage.AGV_EN_ROUTE, Stage.AT_STS, Stage.AGV_TO_TP, Stage.TO_STS, Stage.AT_STS_LOAD}
)
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

# Where an order goes when its running job fails. Stages missing here keep their stage
# (the box is on the AGV/at the TP) and simply get the job re-issued by ``tick()``.
ROLLBACK = {
    Stage.AGV_EN_ROUTE: Stage.AWAIT_AGV,
    Stage.DISCHARGING: Stage.AT_STS,
    Stage.UNSTACKING: Stage.AWAIT_ASC,
    Stage.AGV_TO_TP: Stage.AT_TP,
    Stage.LOADING: Stage.AT_STS_LOAD,
}
# Rollbacks where the AGV carries no box yet and is released.
RELEASE_AGV = frozenset({Stage.AGV_EN_ROUTE, Stage.AGV_TO_TP})


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
    job_counts: dict[JobType, int] = field(default_factory=dict)

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
