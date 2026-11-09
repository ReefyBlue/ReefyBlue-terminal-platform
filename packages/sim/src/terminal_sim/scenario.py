from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from terminal_model.types import Kind


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


def load_scenario(path: Path) -> Scenario:
    raw = yaml.safe_load(Path(path).read_text())
    fault_raw = raw.get("faults") or {}
    faults = {
        kind: FaultParams(
            mtbf_s=float((fault_raw.get(kind.value.lower()) or {}).get("mtbf_s", 0)),
            mttr_s=float((fault_raw.get(kind.value.lower()) or {}).get("mttr_s", 0)),
        )
        for kind in Kind
    }
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
    if not 1 <= s.n_sts <= 2:
        raise ValueError(f"n_sts must be 1..2, got {s.n_sts}")
    if not 1 <= s.n_agv <= 6:
        raise ValueError(f"n_agv must be 1..6, got {s.n_agv}")
    if s.n_containers < 1:
        raise ValueError(f"n_containers must be >= 1, got {s.n_containers}")
    if not 0 <= s.share_40ft <= 1:
        raise ValueError(f"share_40ft must be within 0..1, got {s.share_40ft}")
    return s
