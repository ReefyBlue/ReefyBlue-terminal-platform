from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    STS = "STS"
    AGV = "AGV"
    ASC = "ASC"


class State(StrEnum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    FAULT = "FAULT"
    MAINTENANCE = "MAINTENANCE"


class JobType(StrEnum):
    DISCHARGE = "DISCHARGE"
    LOAD = "LOAD"
    STACK_IN = "STACK_IN"
    STACK_OUT = "STACK_OUT"
    TRANSPORT = "TRANSPORT"


class JobStatus(StrEnum):
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Position:
    x: float
    y: float

    def distance_to(self, other: Position) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass
class Job:
    job_id: str
    type: JobType
    container_id: str
    from_loc: str
    to_loc: str
    priority: int = 0
    deadline: float | None = None
    status: JobStatus = JobStatus.ASSIGNED


@dataclass(frozen=True)
class Container:
    container_id: str
    length_ft: int
    weight_t: float
    dest_block: str

    def __post_init__(self) -> None:
        if self.length_ft not in (20, 40):
            raise ValueError(f"length_ft must be 20 or 40, got {self.length_ft}")
