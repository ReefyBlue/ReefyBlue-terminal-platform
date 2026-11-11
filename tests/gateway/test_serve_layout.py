"""``load_gateway_layout`` reads equipment ids from the legacy and the v2 layout files."""

from __future__ import annotations

from pathlib import Path

from terminal_gateway.gateway import Gateway, upstream_table
from terminal_gateway.serve import load_gateway_layout
from terminal_sim.layout import load_layout

ROOT = Path(__file__).resolve().parents[2]


def test_legacy_layout():
    g = load_gateway_layout(ROOT / "terminal.yaml")
    assert [s.equipment_id for s in g.sts] == ["STS-01", "STS-02"]
    assert len(g.blocks) == 4
    assert (g.opcua_base_port, g.opcua_host) == (48400, "localhost")


def test_v2_layout_matches_the_sim_view():
    for name in ("terminal_mv_small.yaml", "terminal_mv.yaml"):
        g = load_gateway_layout(ROOT / name)
        sim = load_layout(ROOT / name)
        assert [s.equipment_id for s in g.sts] == [s.equipment_id for s in sim.sts]
        assert [(b.block, b.asc_id) for b in g.blocks] == [(b.block, b.asc_id) for b in sim.blocks]
        assert (g.opcua_base_port, g.opcua_host) == (sim.opcua_base_port, sim.opcua_host)


def test_v2_small_upstream_table():
    g = load_gateway_layout(ROOT / "terminal_mv_small.yaml")
    ids = [u.equipment_id for u in upstream_table(g, base_port=0, n_sts=3, n_agv=5)]
    assert ids[:3] == ["STS-01", "STS-02", "STS-03"]
    assert ids[-4:] == [b.asc_id for b in g.blocks]


def test_control_slot_follows_the_fleet():
    """Clock server at ``base_port + n_equipment``, as in the simulator."""
    legacy = load_gateway_layout(ROOT / "terminal.yaml")
    small = load_gateway_layout(ROOT / "terminal_mv_small.yaml")
    assert Gateway(legacy, upstream_host="h", base_port=30000).control_port == 30012  # 2 + 6 + 4
    assert Gateway(legacy, upstream_host="h", base_port=30000, n_sts=1, n_agv=0).control_port == 30005
    assert Gateway(small, upstream_host="h", base_port=30000, n_sts=3, n_agv=12).control_port == 30019
