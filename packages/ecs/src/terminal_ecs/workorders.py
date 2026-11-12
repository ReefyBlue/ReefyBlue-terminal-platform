"""Work orders submitted to the ECS by the TOS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from terminal_model.types import JobType


@dataclass(frozen=True)
class WorkOrder:
    container_id: str
    type: JobType  # DISCHARGE or LOAD; rack mode also TRUCK_IN / TRUCK_OUT (LS-ASC, no STS)
    sts_id: str  # unused ("") for TRUCK_IN / TRUCK_OUT
    yard_slot: str
    # Rack mode (v2 layouts) only; ignored by the legacy dispatcher.
    reefer: bool = False  # reefer box: reefer racks only
    alternative_blocks: tuple[str, ...] = ()  # discharge: blocks to re-block to when no rack is free
    vessel_slot: str | None = None  # VESSEL:<berth>:<bay>:<row>:<tier>; None = the dispatcher picks


class SlotProvider(Protocol):
    def next_slot(self, block: str, exclude: str | None = None) -> str | None:
        """Return a free slot location string in ``block``, or None if the yard is full."""
        ...


class WorkOrderError(ValueError):
    """Rejected work order; ``code`` is DUPLICATE_CONTAINER, UNKNOWN_STS or INVALID_SLOT."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
