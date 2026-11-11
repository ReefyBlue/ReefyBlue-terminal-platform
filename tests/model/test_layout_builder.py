from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from terminal_model.layout import ZONE_TYPES, LayoutError, load_terminal, validate
from terminal_model.layout_builder import standard_terminal, to_yaml_dict

ROOT = Path(__file__).resolve().parents[2]


def _small():
    return standard_terminal(name="mv_small", quay_length_m=400, n_berths=1, n_sts=3, n_blocks=4,
                             n_agv_buffers_per_sts=4)


def _demo():
    return standard_terminal(name="mv", quay_length_m=1200, n_berths=2, n_sts=8, n_blocks=24)


def test_small_counts():
    t = _small()
    validate(t)
    assert len(t.sts) == 3
    assert len(t.blocks) == 4
    assert len(t.transfer_zones) == 4
    assert all(len(z.racks) == 6 for z in t.transfer_zones)
    assert len(t.apron.buffers) == 12
    assert t.depth_m == 460.0


def test_demo_counts():
    t = _demo()
    validate(t)
    assert len(t.sts) == 8
    assert len(t.blocks) == 24
    assert sum(len(z.racks) for z in t.transfer_zones) == 144
    assert len({i for b in t.blocks for i in (b.asc_ws_id, b.asc_ls_id)}) == 48
    assert t.blocks[-1].x1_m == pytest.approx(1020.0)
    assert t.depth_m == 460.0


def test_demo_matches_spec_values():
    t = _demo()
    assert [s.x0_m for s in t.sts] == [75 + 150 * n for n in range(8)]
    assert [(b.id, b.from_m, b.to_m) for b in t.quay.berths] == [("A", 0, 600), ("B", 600, 1200)]
    assert t.sts[0].berth_ids == ("A",) and t.sts[7].berth_ids == ("B",)
    assert [ln.x_m for ln in t.sts[0].lanes] == [71.5, 75.0, 78.5]
    assert all(ln.y_m == 45 and ln.len_m == 16 and ln.width_m == 3.5 for ln in t.sts[0].lanes)
    assert [(d.y_m, d.direction) for d in t.apron.drive_lanes] == [
        (70, "E"), (76, "W"), (82, "E"), (88, "W")]
    assert t.apron.crossings_every_m == 50
    assert [b.x_m for b in t.apron.buffers if b.sts_id == "STS-01"] == [67.0, 83.0]
    assert [b.id for b in t.blocks] == list("ABCDEFGHIJKLMNOPQRSTUVWX")
    assert [b.x0_m for b in t.blocks] == [25 + 42 * i for i in range(24)]
    a = t.block("A")
    assert (a.y0_m, a.bays, a.rows, a.tiers, a.asc_ws_id, a.asc_ls_id, a.reefer_rows) == (
        165, 40, 10, 5, "ASC-A-WS", "ASC-A-LS", (9,))
    racks = t.transfer_zone("A").racks
    assert [k.x_m for k in racks] == [32.5, 36.0, 39.5, 43.0, 46.5, 53.5]
    assert [k.reefer for k in racks] == [False] * 5 + [True]
    assert all(k.y_m == 135 and k.size == "40" for k in racks)
    assert all((h.y_m, h.lanes, h.len_m) == (425, 4, 35) for h in t.landside)


def test_small_matches_spec_values():
    t = _small()
    assert [s.x0_m for s in t.sts] == [66.667, 200.0, 333.333]
    assert [b.x_m - t.sts[0].x0_m for b in t.apron.buffers if b.sts_id == "STS-01"] == pytest.approx(
        [-8, 8, -16, 16])
    assert [b.id for b in t.apron.buffers][:4] == ["BUF-01-1", "BUF-01-2", "BUF-01-3", "BUF-01-4"]
    assert [b.x0_m for b in t.blocks] == [25.0, 67.0, 109.0, 151.0]
    assert t.blocks[-1].x1_m == pytest.approx(180.0)
    assert [(b.id, b.from_m, b.to_m) for b in t.quay.berths] == [("A", 0.0, 400.0)]


def test_zones_cover_spec_table():
    t = _demo()
    types = {z.type for z in t.zones}
    assert types == ZONE_TYPES
    assert sum(z.type == "block" for z in t.zones) == 24
    assert sum(z.type == "transfer_ws" for z in t.zones) == 24
    assert sum(z.type == "truck_interchange" for z in t.zones) == 24
    assert sum(z.type == "maintenance_lane" for z in t.zones) == 23
    byname = {z.name: z for z in t.zones}
    assert byname["oog"].polygon[0] == (1040.0, 165.0)
    assert byname["rail-interchange"].polygon[0][1] == 480.0
    assert byname["rail-terminal"].polygon[2][1] == 580.0
    assert sum(z.type == "building" for z in t.zones) == 2
    assert all(len(z.polygon) == 4 for z in t.zones)


def test_yaml_roundtrip(tmp_path):
    for t in (_small(), _demo()):
        p = tmp_path / f"{t.name}.yaml"
        p.write_text(yaml.safe_dump(to_yaml_dict(t), sort_keys=False))
        assert load_terminal(p) == t


def test_to_yaml_dict_is_deterministic_and_plain():
    d = to_yaml_dict(_small())
    assert d["version"] == 2 and list(d)[:2] == ["version", "name"]
    assert d == to_yaml_dict(_small())

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                assert type(k) is str
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        else:
            assert type(x) in (str, int, float, bool), (x, type(x))

    walk(d)


@pytest.mark.parametrize("fname, build", [("terminal_mv_small.yaml", _small), ("terminal_mv.yaml", _demo)])
def test_shipped_files_match_builder(fname, build):
    assert load_terminal(ROOT / fname) == build()
    assert yaml.safe_load((ROOT / fname).read_text()) == to_yaml_dict(build())


def test_validate_rejects_bad_enums():
    t = _small()
    bad = replace(
        t,
        apron=replace(t.apron, drive_lanes=(replace(t.apron.drive_lanes[0], direction="N"),)
                      + t.apron.drive_lanes[1:]),
        transfer_zones=(replace(t.transfer_zones[0], racks=(replace(t.transfer_zones[0].racks[0], size="30"),)
                                + t.transfer_zones[0].racks[1:]),) + t.transfer_zones[1:],
        blocks=(replace(t.blocks[0], maintenance_lane_side="S"),) + t.blocks[1:],
    )
    with pytest.raises(LayoutError) as e:
        validate(bad)
    msg = str(e.value)
    assert "direction 'N'" in msg and "size '30'" in msg and "maintenance_lane_side 'S'" in msg
