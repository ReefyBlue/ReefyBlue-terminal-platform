from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from terminal_model.types import Kind

if TYPE_CHECKING:
    from terminal_sim.layout import Layout


@dataclass(frozen=True)
class FaultParams:
    mtbf_s: float
    mttr_s: float  # 0 mtbf = no faults


@dataclass(frozen=True)
class Scenario:
    name: str
    seed: int
    n_sts: int
    n_agv: int
    n_containers: int
    share_40ft: float
    discharge_share: float
    faults: dict[Kind, FaultParams]
    speed_factor: float
    duration_s: float


def load_scenario(path: Path, layout: Layout | None = None) -> Scenario:
    """Load and validate a scenario. With ``layout`` the fleet is capped by it
    (``n_sts <= len(layout.sts)``, ``n_agv <= len(layout.agv_start_cells)``); without one
    only ``>= 1`` is checked."""
    raw = yaml.safe_load(Path(path).read_text())
    fault_raw = raw.get("faults") or {}
    kinds = {kind.value.lower() for kind in Kind}
    for key, params in fault_raw.items():
        if key not in kinds:
            raise ValueError(f"unknown fault key faults.{key} (expected one of {sorted(kinds)})")
        for field in params or {}:
            if field not in ("mtbf_s", "mttr_s"):
                raise ValueError(f"unknown fault key faults.{key}.{field} (expected mtbf_s, mttr_s)")
    faults = {
        kind: FaultParams(
            mtbf_s=float((fault_raw.get(kind.value.lower()) or {}).get("mtbf_s", 0)),
            mttr_s=float((fault_raw.get(kind.value.lower()) or {}).get("mttr_s", 0)),
        )
        for kind in Kind
    }
    for kind, fp in faults.items():
        name = kind.value.lower()
        if fp.mtbf_s < 0:
            raise ValueError(f"faults.{name}.mtbf_s must be >= 0, got {fp.mtbf_s}")
        if fp.mttr_s < 0:
            raise ValueError(f"faults.{name}.mttr_s must be >= 0, got {fp.mttr_s}")
        if fp.mtbf_s > 0 and fp.mttr_s == 0:
            raise ValueError(
                f"faults.{name}.mttr_s must be > 0 when mtbf_s > 0"
            )
    s = Scenario(
        name=raw["name"],
        seed=int(raw["seed"]),
        n_sts=int(raw["n_sts"]),
        n_agv=int(raw["n_agv"]),
        n_containers=int(raw["n_containers"]),
        share_40ft=float(raw["share_40ft"]),
        discharge_share=float(raw["discharge_share"]),
        faults=faults,
        speed_factor=float(raw["speed_factor"]),
        duration_s=float(raw["duration_s"]),
    )
    for field, value, limit in (
        ("n_sts", s.n_sts, None if layout is None else len(layout.sts)),
        ("n_agv", s.n_agv, None if layout is None else len(layout.agv_start_cells)),
    ):
        if value < 1:
            raise ValueError(f"{field} must be >= 1, got {value}")
        if limit is not None and value > limit:
            raise ValueError(f"{field} must be 1..{limit} for layout, got {value}")
    if s.n_containers < 1:
        raise ValueError(f"n_containers must be >= 1, got {s.n_containers}")
    if not 0 <= s.share_40ft <= 1:
        raise ValueError(f"share_40ft must be within 0..1, got {s.share_40ft}")
    return s
