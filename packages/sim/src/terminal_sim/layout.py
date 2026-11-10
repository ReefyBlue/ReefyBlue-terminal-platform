from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from terminal_model.types import Position

Cell = tuple[int, int]  # (col, row)


@dataclass(frozen=True)
class StsSpec:
    equipment_id: str
    position: Position
    lane_cells: tuple[Cell, Cell]


@dataclass(frozen=True)
class BlockSpec:
    block: str
    asc_id: str
    tp_ws: Position
    tp_ws_cell: Cell
    bays: int
    rows: int
    tiers: int


@dataclass(frozen=True)
class Layout:
    quay_length_m: float
    grid_cell_m: float
    grid_cols: int
    grid_rows: int
    sts: list[StsSpec]
    blocks: list[BlockSpec]
    agv_start_cells: list[Cell]


def _cell(v) -> Cell:
    return (int(v[0]), int(v[1]))


def load_layout(path: Path) -> Layout:
    raw = yaml.safe_load(Path(path).read_text())
    sts = [
        StsSpec(
            equipment_id=s["id"],
            position=Position(float(s["x"]), float(s["y"])),
            lane_cells=(_cell(s["lane_cells"][0]), _cell(s["lane_cells"][1])),
        )
        for s in raw["sts"]
    ]
    blocks = [
        BlockSpec(
            block=b["block"],
            asc_id=b["asc_id"],
            tp_ws=Position(float(b["tp_ws"]["x"]), float(b["tp_ws"]["y"])),
            tp_ws_cell=_cell(b["tp_ws_cell"]),
            bays=int(b["bays"]),
            rows=int(b["rows"]),
            tiers=int(b["tiers"]),
        )
        for b in raw["blocks"]
    ]
    return Layout(
        quay_length_m=float(raw["quay"]["length_m"]),
        grid_cell_m=float(raw["grid"]["cell_m"]),
        grid_cols=int(raw["grid"]["cols"]),
        grid_rows=int(raw["grid"]["rows"]),
        sts=sts,
        blocks=blocks,
        agv_start_cells=[_cell(c) for c in raw["agv_start_cells"]],
    )
