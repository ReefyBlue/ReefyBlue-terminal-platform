"""Pydantic schemas of the ECS service API (spec §3; operator endpoints: B2d spec §6)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


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
    version: int = 1  # 1 = legacy terminal.yaml, 2 = v2 terminal model
    terminal: TerminalOut | None = None  # the v2 model (zones inside); None for v1


class _Strict(BaseModel):
    """Base of the TerminalOut sub-models: unknown keys are an error, so a field added to a
    ``terminal_model.layout`` dataclass fails loudly instead of being dropped silently."""

    model_config = ConfigDict(extra="forbid")


class BerthOut(_Strict):
    id: str
    from_m: float
    to_m: float


class QuayOut(_Strict):
    length_m: float
    berths: list[BerthOut]


class RailsOut(_Strict):
    y_ws_m: float
    y_ls_m: float


class LaneOut(_Strict):
    id: str
    x_m: float
    y_m: float
    len_m: float
    width_m: float


class StsSpecOut(_Strict):
    id: str
    x0_m: float
    min_gap_m: float
    berth_ids: list[str]
    lanes: list[LaneOut]


class DriveLaneOut(_Strict):
    id: str
    y_m: float
    direction: Literal["E", "W"]


class BufferOut(_Strict):
    id: str
    x_m: float
    y_m: float
    sts_id: str


class ApronOut(_Strict):
    y_from_m: float
    y_to_m: float
    drive_lanes: list[DriveLaneOut]
    crossings_every_m: float
    buffers: list[BufferOut]


class RackOut(_Strict):
    id: str
    x_m: float
    y_m: float
    size: Literal["20", "40", "45"]
    reefer: bool = False


class TransferZoneOut(_Strict):
    block: str
    racks: list[RackOut]
    approach_lane_id: str


class TerminalBlockOut(_Strict):
    id: str
    x0_m: float
    y0_m: float
    bays: int
    rows: int
    tiers: int
    asc_ws_id: str
    asc_ls_id: str
    bay_pitch_m: float
    row_pitch_m: float
    tier_h_m: float
    asc_min_gap_bays: int
    maintenance_lane_side: Literal["E", "W"]
    reefer_rows: list[int]


class TruckHeadOut(_Strict):
    block: str
    lanes: int
    y_m: float
    len_m: float


class ZoneOut(_Strict):
    name: str
    type: str
    polygon: list[tuple[float, float]]


class OpcUaOut(_Strict):
    base_port: int
    host: str


class TerminalOut(_Strict):
    """The v2 terminal model (mirrors ``terminal_model.layout.TerminalLayout``; metres)."""

    name: str
    quay: QuayOut
    sts_rails: RailsOut
    sts: list[StsSpecOut]
    apron: ApronOut
    transfer_zones: list[TransferZoneOut]
    blocks: list[TerminalBlockOut]
    landside: list[TruckHeadOut]
    zones: list[ZoneOut]
    opcua: OpcUaOut
    kinematics: dict[str, dict[str, float]] = {}


LayoutOut.model_rebuild()
