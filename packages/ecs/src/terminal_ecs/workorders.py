"""Work orders submitted to the ECS by the TOS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from terminal_model.types import JobType


@dataclass(frozen=True)
class WorkOrder:
    container_id: str
    type: JobType  # DISCHARGE or LOAD
    sts_id: str
    yard_slot: str


class SlotProvider(Protocol):
    def next_slot(self, block: str, exclude: str | None = None) -> str | None:
        """Return a free slot location string in ``block``, or None if the yard is full."""
        ...


class WorkOrderError(ValueError):
    """Rejected work order; ``code`` is DUPLICATE_CONTAINER, UNKNOWN_STS or INVALID_SLOT."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
