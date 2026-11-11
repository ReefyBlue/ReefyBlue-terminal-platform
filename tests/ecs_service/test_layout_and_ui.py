from pathlib import Path

import httpx
from fake_port import FakePort
from terminal_ecs_service.app import create_app
from terminal_ecs_service.core import EcsCore
from terminal_model.types import Kind, Position
from terminal_sim.layout import load_layout

ROOT = Path(__file__).resolve().parents[2]


def _client(layout):
    fake = FakePort({"STS-01": (Kind.STS, Position(100, 0))})
    core = EcsCore(layout, connect=lambda: fake)
    app = create_app(core, run_loop=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_layout_endpoint_mirrors_terminal_yaml():
    layout = load_layout(ROOT / "terminal.yaml")
    async with _client(layout) as c:
        r = await c.get("/layout")
    assert r.status_code == 200
    body = r.json()
    assert body["quay_length_m"] == 400.0 and body["grid_cell_m"] == 20.0
    assert body["grid_cols"] == 20 and body["grid_rows"] == 6
    assert body["sts"][0] == {"equipment_id": "STS-01", "x": 100.0, "y": 0.0, "lane_cells": [[4, 0], [5, 0]]}
    b = body["blocks"][0]
    assert b["block"] == "A" and b["asc_id"] == "ASC-A" and b["tp_ws"] == {"x": 50.0, "y": 120.0}
    assert b["tp_ws_cell"] == [2, 5] and (b["bays"], b["rows"], b["tiers"]) == (40, 6, 4)
    assert body["agv_start_cells"][0] == [0, 2]


async def test_layout_available_without_equipment_link():
    layout = load_layout(ROOT / "terminal.yaml")
    async with _client(layout) as c:  # core never connected
        assert (await c.get("/layout")).status_code == 200
