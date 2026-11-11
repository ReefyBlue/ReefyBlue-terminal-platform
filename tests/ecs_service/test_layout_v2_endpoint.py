from pathlib import Path

import httpx
from fake_port import FakePort
from terminal_ecs_service.app import create_app
from terminal_ecs_service.core import EcsCore
from terminal_ecs_service.serve import load_layout
from terminal_model.layout import ZONE_TYPES, load_terminal
from terminal_model.layout_grid import grid_view
from terminal_model.types import Kind, Position

ROOT = Path(__file__).resolve().parents[2]


def _client(layout):
    fake = FakePort({"STS-01": (Kind.STS, Position(100, 0))})
    core = EcsCore(layout, connect=lambda: fake)
    app = create_app(core, run_loop=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _body(path):
    async with _client(load_layout(path)) as c:
        r = await c.get("/layout")
    assert r.status_code == 200
    return r.json()


async def test_layout_v2_keeps_legacy_fields():
    body = await _body(ROOT / "terminal_mv_small.yaml")
    g = grid_view(load_terminal(ROOT / "terminal_mv_small.yaml"))
    assert body["version"] == 2
    assert body["quay_length_m"] == 400.0
    assert body["grid_cols"] == g.cols == 20 and body["grid_rows"] == g.rows
    assert body["grid_cell_m"] == 20.0
    assert body["sts"][0]["x"] == g.sts[0].x_m and body["sts"][0]["y"] == g.sts[0].y_m
    assert body["sts"][0]["equipment_id"] == g.sts[0].id
    assert body["sts"][0]["lane_cells"] == [list(c) for c in g.sts[0].lane_cells]
    b = body["blocks"][0]
    assert b["tp_ws"] == {"x": g.blocks[0].tp_ws_m[0], "y": g.blocks[0].tp_ws_m[1]}
    assert b["tp_ws_cell"] == list(g.blocks[0].tp_ws_cell)
    assert body["agv_start_cells"] == [list(c) for c in g.agv_start_cells]


async def test_layout_v2_has_terminal():
    body = await _body(ROOT / "terminal_mv_small.yaml")
    t = body["terminal"]
    assert set(t) >= {"name", "quay", "sts_rails", "sts", "apron", "transfer_zones", "blocks",
                      "landside", "zones", "opcua"}
    assert len(t["blocks"]) == 4
    assert len(t["transfer_zones"][0]["racks"]) == 6
    assert t["zones"] and all(z["type"] in ZONE_TYPES for z in t["zones"])


async def test_layout_v1_has_no_terminal():
    body = await _body(ROOT / "terminal.yaml")
    assert body["version"] == 1 and body["terminal"] is None
