from dataclasses import replace
from pathlib import Path

import pytest
from terminal_model.layout import Lane, LayoutError, load_terminal
from terminal_model.layout_grid import cell_of, grid_view

ROOT = Path(__file__).resolve().parents[2]


def _small():
    return load_terminal(ROOT / "terminal_mv_small.yaml")


def _demo():
    return load_terminal(ROOT / "terminal_mv.yaml")


def test_cell_of():
    assert cell_of(0.0, 0.0, 20.0) == (0, 0)
    assert cell_of(19.999, 45.0, 20.0) == (0, 2)
    assert cell_of(20.0, 135.0, 20.0) == (1, 6)


def test_grid_view_small():
    g = grid_view(_small())
    assert (g.cell_m, g.cols, g.rows) == (20.0, 20, 23)
    assert [s.id for s in g.sts] == ["STS-01", "STS-02", "STS-03"]
    assert [s.lane_cells for s in g.sts] == [((3, 2), (4, 2)), ((9, 2), (10, 2)),
                                            ((16, 2), (17, 2))]
    for s in g.sts:
        assert s.lane_cells[0] != s.lane_cells[1]
    assert (g.sts[0].x_m, g.sts[0].y_m) == (66.667, 10.0)
    assert [b.id for b in g.blocks] == ["A", "B", "C", "D"]
    assert [b.asc_id for b in g.blocks] == ["ASC-A-WS", "ASC-B-WS", "ASC-C-WS", "ASC-D-WS"]
    assert [b.tp_ws_cell for b in g.blocks] == [(1, 6), (3, 6), (5, 6), (7, 6)]
    assert g.blocks[0].tp_ws_m == (32.5, 135.0)
    assert (g.blocks[0].bays, g.blocks[0].rows, g.blocks[0].tiers) == (40, 10, 5)
    assert g.agv_start_cells == ((2, 4), (3, 4), (1, 4), (4, 4), (9, 4), (10, 4), (8, 4),
                                 (11, 4), (16, 4), (17, 4), (15, 4), (18, 4))
    assert len(set(g.agv_start_cells)) == 12


def test_grid_view_lane_cells_distinct():
    """Review Focus 3: two lanes 3.5 m apart in one cell still give two distinct cells."""
    t = _small()
    s0 = t.sts[0]
    lanes = (Lane("X-L1", 62.0, 45.0, 16.0, 3.5), Lane("X-L2", 65.5, 45.0, 16.0, 3.5))
    t2 = replace(t, sts=(replace(s0, lanes=lanes),) + t.sts[1:])
    g = grid_view(t2)
    assert g.sts[0].lane_cells == ((3, 2), (4, 2))


def test_grid_view_lane_cell_west_neighbour_at_east_edge():
    t = _small()
    s2 = t.sts[2]
    lanes = (Lane("X-L1", 395.0, 45.0, 16.0, 3.5),)
    t2 = replace(t, sts=t.sts[:2] + (replace(s2, lanes=lanes),))
    g = grid_view(t2)
    assert g.sts[2].lane_cells == ((19, 2), (18, 2))


def test_grid_view_tp_cell_conflict_raises():
    t = _small()
    za, zb = t.transfer_zones[0], t.transfer_zones[1]
    k = zb.racks[0]
    moved = replace(zb, racks=(replace(k, x_m=za.racks[0].x_m),) + zb.racks[1:])
    t2 = replace(t, transfer_zones=(za, moved) + t.transfer_zones[2:])
    with pytest.raises(LayoutError, match="TP cell"):
        grid_view(t2)


def test_grid_view_tp_cell_on_lane_cell_raises():
    t = _small()
    za = t.transfer_zones[0]
    k = za.racks[0]
    moved = replace(za, racks=(replace(k, x_m=70.0, y_m=45.0),) + za.racks[1:])
    t2 = replace(t, transfer_zones=(moved,) + t.transfer_zones[1:])
    with pytest.raises(LayoutError, match="TP cell"):
        grid_view(t2)


def test_grid_view_lane_cells_shared_between_sts_raises():
    t = _small()
    s1 = t.sts[1]
    lanes = tuple(replace(ln, x_m=ln.x_m - 133.333) for ln in s1.lanes)  # onto STS-01's cells
    t2 = replace(t, sts=(t.sts[0], replace(s1, lanes=lanes), t.sts[2]))
    with pytest.raises(LayoutError, match="lane cell"):
        grid_view(t2)


def test_grid_view_demo_tp_cells_distinct():
    g = grid_view(_demo())
    assert (g.cols, g.rows) == (60, 23)
    assert len(g.blocks) == 24
    tp = [b.tp_ws_cell for b in g.blocks]
    assert len(set(tp)) == 24
    lanes = [c for s in g.sts for c in s.lane_cells]
    assert len(set(lanes)) == 16
    assert not set(tp) & set(lanes)
    assert len(set(g.agv_start_cells)) == len(g.agv_start_cells) == 16
    assert not set(g.agv_start_cells) & (set(tp) | set(lanes))
