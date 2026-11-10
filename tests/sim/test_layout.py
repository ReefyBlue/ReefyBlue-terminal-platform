from pathlib import Path

from terminal_model.types import Position
from terminal_sim.layout import load_layout


def test_layout_geometry():
    L = load_layout(Path("terminal.yaml"))
    assert (L.grid_cols, L.grid_rows) == (20, 6)
    assert (L.quay_length_m, L.grid_cell_m) == (400, 20)
    assert L.sts[1].position == Position(250, 0)
    assert L.sts[0].lane_cells == ((4, 0), (5, 0))
    assert L.blocks[1].tp_ws == Position(150, 120)
    assert [b.block for b in L.blocks] == ["A", "B", "C", "D"]
    assert [b.tp_ws_cell for b in L.blocks] == [(2, 5), (7, 5), (12, 5), (17, 5)]
    assert L.blocks[0].bays * L.blocks[0].rows * L.blocks[0].tiers == 960
    assert L.agv_start_cells == [(i, 2) for i in range(6)]
