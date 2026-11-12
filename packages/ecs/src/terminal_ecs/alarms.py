"""Operator alarms derived from the Dispatcher's state (B2d §5.4)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AlarmKind(StrEnum):
    LINK_LOST = "LINK_LOST"  # no link to the device (OFFLINE)
    NEEDS_RELEASE = "NEEDS_RELEASE"  # after a FAULT / unknown outcome: operator must release
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"  # a job's result was lost during a link outage
    NO_FREE_COLUMN = "NO_FREE_COLUMN"  # rack mode, WARNING: an order waits for a free column to dig


@dataclass(frozen=True)
class Alarm:
    kind: AlarmKind
    equipment_id: str
    job_id: str | None
    since: float  # sim time the condition arose
    detail: str
    severity: str = "ERROR"  # "ERROR": the operator must act; "WARNING": informational, blocks nothing


class ReleaseRefused(Exception):
    """``Dispatcher.release`` cannot release the device yet; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
