from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from terminal_model.layout import TerminalLayout, is_v2, load_terminal
from terminal_model.layout_grid import grid_view
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
    opcua_base_port: int = 48400
    opcua_host: str = "localhost"
    # The metre layout behind a v2 file (None for a legacy grid file); ``make_world`` builds a
    # ``World2`` when it is set.
    terminal: TerminalLayout | None = None


def _cell(v) -> Cell:
    return (int(v[0]), int(v[1]))


def _from_v2(path: Path) -> Layout:
    """A v2 (metre) layout seen through its derived grid (``terminal_model.layout_grid``)."""
    t = load_terminal(path)
    g = grid_view(t)
    return Layout(
        quay_length_m=t.quay.length_m,
        grid_cell_m=g.cell_m,
        grid_cols=g.cols,
        grid_rows=g.rows,
        sts=[StsSpec(equipment_id=s.id, position=Position(s.x_m, s.y_m), lane_cells=s.lane_cells)
             for s in g.sts],
        blocks=[
            BlockSpec(block=b.id, asc_id=b.asc_id, tp_ws=Position(*b.tp_ws_m),
                      tp_ws_cell=b.tp_ws_cell, bays=b.bays, rows=b.rows, tiers=b.tiers)
            for b in g.blocks
        ],
        agv_start_cells=list(g.agv_start_cells),
        opcua_base_port=t.opcua.base_port,
        opcua_host=t.opcua.host,
        terminal=t,
    )


def load_layout(path: Path) -> Layout:
    raw = yaml.safe_load(Path(path).read_text())
    if isinstance(raw, dict) and is_v2(raw):
        return _from_v2(path)
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
    opcua = raw.get("opcua") or {}
    return Layout(
        quay_length_m=float(raw["quay"]["length_m"]),
        grid_cell_m=float(raw["grid"]["cell_m"]),
        grid_cols=int(raw["grid"]["cols"]),
        grid_rows=int(raw["grid"]["rows"]),
        sts=sts,
        blocks=blocks,
        agv_start_cells=[_cell(c) for c in raw["agv_start_cells"]],
        opcua_base_port=int(opcua.get("base_port", 48400)),
        opcua_host=str(opcua.get("host", "localhost")),
    )
