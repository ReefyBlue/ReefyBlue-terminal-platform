from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from terminal_model.types import Job, Kind, Position, State


@dataclass(frozen=True)
class EquipmentSnapshot:
    equipment_id: str
    kind: Kind
    vendor: str
    state: State
    position: Position
    current_job_id: str | None
    energy_kwh: float
    extra: Mapping[str, object]


class EventKind(StrEnum):
    STATE_CHANGED = "STATE_CHANGED"
    JOB_COMPLETED = "JOB_COMPLETED"
    JOB_FAILED = "JOB_FAILED"


class LinkReason(StrEnum):
    LINK_LOST = "LINK_LOST"
    LINK_RESTORED = "LINK_RESTORED"
    UPSTREAM_DISCONNECTED = "UPSTREAM_DISCONNECTED"
    UPSTREAM_RECONNECTED = "UPSTREAM_RECONNECTED"


@dataclass(frozen=True)
class EquipmentEvent:
    kind: EventKind
    equipment_id: str
    sim_time: float
    job_id: str | None = None
    reason: str | None = None
    new_state: State | None = None


@runtime_checkable
class EquipmentPort(Protocol):
    def equipment_ids(self) -> list[str]: ...

    def snapshot(self, equipment_id: str) -> EquipmentSnapshot: ...

    def assign_job(self, equipment_id: str, job: Job) -> bool: ...

    def cancel_job(self, equipment_id: str) -> bool: ...

    def reset(self, equipment_id: str) -> bool: ...

    def subscribe(self, callback: Callable[[EquipmentEvent], None]) -> None: ...

    def now(self) -> float: ...
