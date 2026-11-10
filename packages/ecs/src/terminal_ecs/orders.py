"""Per-order book-keeping for the dispatcher: stages, the order record and pure helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from terminal_model.locations import SlotLoc, parse_location
from terminal_model.types import JobType

from terminal_ecs.workorders import WorkOrder


class Stage(StrEnum):
    AWAIT_AGV = "AWAIT_AGV"
    AGV_EN_ROUTE = "AGV_EN_ROUTE"
    AT_STS = "AT_STS"
    DISCHARGING = "DISCHARGING"
    TO_YARD = "TO_YARD"
    STACKING = "STACKING"
    DONE = "DONE"
    FAILED = "FAILED"


LOOKAHEAD_STAGES = (Stage.AGV_EN_ROUTE, Stage.AT_STS)
PRE_DISCHARGE = (Stage.AWAIT_AGV, Stage.AGV_EN_ROUTE, Stage.AT_STS)


@dataclass
class OrderRecord:
    order: WorkOrder
    yard_slot: str
    stage: Stage = Stage.AWAIT_AGV
    agv_id: str | None = None
    current_job_id: str | None = None
    seq: int = 0  # set on every stage entry; FIFO key for waiting orders
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
