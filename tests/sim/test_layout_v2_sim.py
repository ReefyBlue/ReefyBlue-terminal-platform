from pathlib import Path

from terminal_model.layout import load_terminal
from terminal_model.layout_grid import grid_view
from terminal_model.types import Position
from terminal_sim.layout import BlockSpec, Layout, StsSpec, load_layout

ROOT = Path(__file__).resolve().parents[2]


def test_sim_load_layout_v2_matches_grid_view():
    L = load_layout(ROOT / "terminal_mv_small.yaml")
    g = grid_view(load_terminal(ROOT / "terminal_mv_small.yaml"))
    assert (L.quay_length_m, L.grid_cell_m, L.grid_cols, L.grid_rows) == (400.0, g.cell_m, g.cols, g.rows)
    assert L.sts == [StsSpec(s.id, Position(s.x_m, s.y_m), s.lane_cells) for s in g.sts]
    assert L.blocks == [
        BlockSpec(block=b.id, asc_id=b.asc_id, tp_ws=Position(*b.tp_ws_m), tp_ws_cell=b.tp_ws_cell,
                  bays=b.bays, rows=b.rows, tiers=b.tiers)
        for b in g.blocks
    ]
    assert L.agv_start_cells == list(g.agv_start_cells)
    assert (L.opcua_base_port, L.opcua_host) == (48400, "localhost")


def test_sim_load_layout_v1_unchanged():
    def blk(b, x, col):
        return BlockSpec(block=b, asc_id=f"ASC-{b}", tp_ws=Position(x, 120), tp_ws_cell=(col, 5),
                         bays=40, rows=6, tiers=4)

    assert load_layout(ROOT / "terminal.yaml") == Layout(
        quay_length_m=400.0, grid_cell_m=20.0, grid_cols=20, grid_rows=6,
        sts=[StsSpec("STS-01", Position(100, 0), ((4, 0), (5, 0))),
             StsSpec("STS-02", Position(250, 0), ((12, 0), (13, 0)))],
        blocks=[blk("A", 50, 2), blk("B", 150, 7), blk("C", 250, 12), blk("D", 350, 17)],
        agv_start_cells=[(i, 2) for i in range(6)],
        opcua_base_port=48400, opcua_host="localhost",
    )
