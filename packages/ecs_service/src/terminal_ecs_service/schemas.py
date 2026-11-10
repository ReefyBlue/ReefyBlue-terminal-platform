"""Pydantic schemas of the ECS service API (spec §3)."""

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
