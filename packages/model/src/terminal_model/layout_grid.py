"""Derived grid view of a metre-based ``TerminalLayout`` (v2).

The grid is a view, not a source: it projects the metre layout onto ``cell_m`` cells so the
legacy grid router, dispatcher and viewer keep working (spec C1 §4).

- ``cols = ceil(quay_length_m / cell_m)``, ``rows = ceil(depth_m / cell_m)``.
- STS lane cells: the cells of the STS's lane centres (``x_m``, ``y_m``) in lane order,
  deduplicated; the first two are used. When all lanes fall in one cell, that cell and its
  east neighbour (west neighbour at the east edge) are used.
- ``tp_ws_cell``: the cell of rack 1 of the block's transfer zone; ``asc_id`` = ``asc_ws_id``.
- ``agv_start_cells``: the buffer cells in STS order; when a cell is taken (two buffers in
  one cell) the nearest free cell on the same row is used, east before west.

Handover cells (all STS lane cells and TP cells) must be pairwise distinct and start cells
must not be handover cells; violations raise ``LayoutError``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from terminal_model.layout import LayoutError, TerminalLayout

Cell = tuple[int, int]  # (col, row), 0-based

_EPS = 1e-9


@dataclass(frozen=True)
class GridSts:
    id: str
    x_m: float
    y_m: float
    lane_cells: tuple[Cell, Cell]


@dataclass(frozen=True)
class GridBlock:
    id: str
    asc_id: str
    tp_ws_m: tuple[float, float]
    tp_ws_cell: Cell
    bays: int
    rows: int
    tiers: int


@dataclass(frozen=True)
class GridView:
    cell_m: float
    cols: int
    rows: int
    sts: tuple[GridSts, ...]
    blocks: tuple[GridBlock, ...]
    agv_start_cells: tuple[Cell, ...]


def cell_of(x_m: float, y_m: float, cell_m: float) -> Cell:
    return (math.floor(x_m / cell_m), math.floor(y_m / cell_m))


def _ceil(v: float) -> int:
    return math.ceil(v - _EPS)


def grid_view(t: TerminalLayout, cell_m: float = 20.0) -> GridView:
    if cell_m <= 0:
        raise LayoutError(f"cell_m must be > 0, got {cell_m}")
    cols = _ceil(t.quay.length_m / cell_m)
    rows = _ceil(t.depth_m / cell_m)
    errs: list[str] = []

    def inside(c: Cell) -> bool:
        return 0 <= c[0] < cols and 0 <= c[1] < rows

    def check(what: str, c: Cell) -> None:
        if not inside(c):
            errs.append(f"{what} cell {c} outside the {cols}x{rows} grid")

    sts: list[GridSts] = []
    for s in t.sts:
        cells: list[Cell] = []
        for ln in s.lanes:
            c = cell_of(ln.x_m, ln.y_m, cell_m)
            if c not in cells:
                cells.append(c)
        if not cells:
            errs.append(f"STS '{s.id}' has no lane")
            continue
        if len(cells) == 1:
            c = cells[0]
            cells.append((c[0] + 1, c[1]) if c[0] + 1 <= cols - 1 else (c[0] - 1, c[1]))
        lane_cells = (cells[0], cells[1])
        for c in lane_cells:
            check(f"STS '{s.id}' lane", c)
        sts.append(GridSts(s.id, s.x0_m, t.sts_rails.y_ws_m, lane_cells))

    blocks: list[GridBlock] = []
    for b in t.blocks:
        zone = t.transfer_zone(b.id)
        if not zone.racks:
            errs.append(f"transfer zone of block '{b.id}' has no rack")
            continue
        rack = zone.racks[0]
        tp_cell = cell_of(rack.x_m, rack.y_m, cell_m)
        check(f"block '{b.id}' TP", tp_cell)
        blocks.append(GridBlock(b.id, b.asc_ws_id, (rack.x_m, rack.y_m), tp_cell,
                                b.bays, b.rows, b.tiers))

    # handover cells: every lane cell and TP cell has exactly one owner
    owner: dict[Cell, str] = {}
    for s in sts:
        for c in s.lane_cells:
            if c in owner:
                errs.append(f"STS '{s.id}' lane cell {c} already used by {owner[c]}")
            else:
                owner[c] = f"STS '{s.id}'"
    for b in blocks:
        if b.tp_ws_cell in owner:
            errs.append(f"block '{b.id}' TP cell {b.tp_ws_cell} already used by "
                        f"{owner[b.tp_ws_cell]}")
        else:
            owner[b.tp_ws_cell] = f"block '{b.id}' TP"

    # AGV start cells: buffer cells in STS order, made distinct along the row
    order = {s.id: i for i, s in enumerate(t.sts)}
    buffers = sorted(t.apron.buffers, key=lambda buf: order.get(buf.sts_id, len(order)))
    taken = set(owner)
    starts: list[Cell] = []
    for buf in buffers:
        c0 = cell_of(buf.x_m, buf.y_m, cell_m)
        found: Cell | None = c0 if inside(c0) and c0 not in taken else None
        for d in range(1, cols):
            if found is not None:
                break
            for c in ((c0[0] + d, c0[1]), (c0[0] - d, c0[1])):
                if inside(c) and c not in taken:
                    found = c
                    break
        if found is None:
            errs.append(f"buffer '{buf.id}' at cell {c0}: no free start cell on row {c0[1]}")
            continue
        taken.add(found)
        starts.append(found)

    if errs:
        raise LayoutError("grid view: " + "\n".join(errs))
    return GridView(cell_m=float(cell_m), cols=cols, rows=rows, sts=tuple(sts),
                    blocks=tuple(blocks), agv_start_cells=tuple(starts))
