"""Pydantic schemas of the ECS service API (spec §3; operator endpoints: B2d spec §6)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class WorkOrderIn(BaseModel):
    container_id: str
    type: Literal["DISCHARGE", "LOAD"]
    sts_id: str
    yard_slot: str


class WorkOrderErrorOut(BaseModel):
    container_id: str | None
    code: str
    message: str


class WorkOrderErrorsOut(BaseModel):
    errors: list[WorkOrderErrorOut]


class AcceptedOut(BaseModel):
    accepted: int


class HealthOut(BaseModel):
    connected: bool
    sim_time: float | None
    error: str | None = None  # "sim_time_reversed": the sim restarted under this ECS


class AlarmOut(BaseModel):
    kind: Literal["LINK_LOST", "NEEDS_RELEASE", "UNKNOWN_OUTCOME"]
    equipment_id: str
    job_id: str | None
    since_sim_time: float
    detail: str


class ReleaseOut(BaseModel):
    released: str


class ResolveIn(BaseModel):
    outcome: Literal["completed", "failed"]


class ResolveOut(BaseModel):
    job_id: str
    outcome: Literal["completed", "failed"]


class RefusedOut(BaseModel):
    """404/409 of the operator endpoints: ``UNKNOWN_EQUIPMENT``, ``UNKNOWN_JOB``, ``RELEASE_REFUSED``."""

    code: str
    message: str
