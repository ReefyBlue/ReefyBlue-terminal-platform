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
    rss_mb: float | None = None  # peak resident set size of the service process (MiB)
    uptime_s: float | None = None  # wall seconds since the service started


class ContainerOut(BaseModel):
    seq: int  # number of the order's last open/done/failed change
    container_id: str
    state: Literal["open", "done", "failed"]
    job_type: Literal["DISCHARGE", "LOAD"]
    yard_slot: str


class ContainersOut(BaseModel):
    """``GET /containers?since=N``: changes after ``N`` in ``seq`` order; ``next`` is
    the ``since`` of the following call."""

    next: int
    items: list[ContainerOut]


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


class PositionOut(BaseModel):
    x: float
    y: float


class StsOut(BaseModel):
    equipment_id: str
    x: float
    y: float
    lane_cells: tuple[tuple[int, int], tuple[int, int]]


class BlockOut(BaseModel):
    block: str
    asc_id: str
    tp_ws: PositionOut
    tp_ws_cell: tuple[int, int]
    bays: int
    rows: int
    tiers: int


class LayoutOut(BaseModel):
    """Static terminal geometry for the viewer (``GET /layout``)."""

    quay_length_m: float
    grid_cell_m: float
    grid_cols: int
    grid_rows: int
    sts: list[StsOut]
    blocks: list[BlockOut]
    agv_start_cells: list[tuple[int, int]]
