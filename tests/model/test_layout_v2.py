from pathlib import Path

import pytest
import yaml

from terminal_model.layout import (
    LayoutError,
    TerminalLayout,
    is_v2,
    load_terminal,
)

MINI_V2 = """
version: 2
name: mini
quay:
  length_m: 200
  berths:
    - {id: A, from_m: 0, to_m: 200}
sts_rails: {y_ws_m: 10, y_ls_m: 40}
sts:
  - id: STS-01
    x0_m: 75
    min_gap_m: 27
    berth_ids: [A]
    lanes:
      - {id: STS-01-L1, x_m: 70, y_m: 45, len_m: 16, width_m: 3.5}
      - {id: STS-01-L2, x_m: 80, y_m: 45, len_m: 16, width_m: 3.5}
apron:
  y_from_m: 40
  y_to_m: 165
  drive_lanes:
    - {id: DL1, y_m: 70, direction: E}
  crossings_every_m: 50
  buffers:
    - {id: BUF-01-1, x_m: 75, y_m: 95, sts_id: STS-01}
transfer_zones:
  - block: A
    approach_lane_id: DL1
    racks:
      - {id: RK-A-1, x_m: 30, y_m: 110, size: "40"}
      - {id: RK-A-2, x_m: 33.5, y_m: 110, size: "20", reefer: true}
blocks:
  - id: A
    x0_m: 25
    y0_m: 165
    bays: 40
    rows: 10
    tiers: 5
    asc_ws_id: ASC-A-WS
    asc_ls_id: ASC-A-LS
landside:
  - {block: A, lanes: 4, y_m: 525}
zones:
  - name: OOG
    type: oog
    polygon: [[0, 0], [10, 0], [10, 10]]
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "t.yaml"
    p.write_text(text)
    return p


def _mutated(tmp_path: Path, fn) -> Path:
    raw = yaml.safe_load(MINI_V2)
    fn(raw)
    return _write(tmp_path, yaml.safe_dump(raw))


def test_load_mini_v2_roundtrip(tmp_path):
    t = load_terminal(_write(tmp_path, MINI_V2))
    assert isinstance(t, TerminalLayout)
    assert t.name == "mini"
    assert t.quay.length_m == 200 and t.quay.berths[0].id == "A"
    assert t.sts_rails.y_ls_m == 40
    sts = t.sts[0]
    assert (sts.id, sts.x0_m, sts.min_gap_m, sts.berth_ids) == ("STS-01", 75, 27, ("A",))
    assert len(sts.lanes) == 2 and sts.lanes[0].width_m == 3.5
    assert t.apron.drive_lanes[0].direction == "E"
    assert t.apron.buffers[0].sts_id == "STS-01"
    rz = t.transfer_zone("A")
    assert rz.approach_lane_id == "DL1" and rz.racks[1].reefer is True
    assert rz.racks[0].size == "40" and rz.racks[0].reefer is False
    assert t.landside[0].lanes == 4
    assert t.zones[0].polygon == ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0))
    assert t.opcua.base_port == 48400 and t.opcua.host == "localhost"
    assert t.block("A").asc_ws_id == "ASC-A-WS"
    assert t.depth_m == 525.0


def test_block_properties(tmp_path):
    b = load_terminal(_write(tmp_path, MINI_V2)).block("A")
    assert b.width_m == pytest.approx(29.0)
    assert b.length_m == pytest.approx(260.0)
    assert b.x1_m == pytest.approx(25 + 29.0)
    assert b.y1_m == pytest.approx(165 + 260.0)
    assert b.maintenance_lane_side == "E" and b.reefer_rows == ()


def test_overlapping_blocks_rejected(tmp_path):
    def f(raw):
        raw["blocks"].append({**raw["blocks"][0], "id": "B", "x0_m": 40,
                              "asc_ws_id": "ASC-B-WS", "asc_ls_id": "ASC-B-LS"})
        raw["transfer_zones"].append({"block": "B", "approach_lane_id": "DL1", "racks": []})
    with pytest.raises(LayoutError) as e:
        load_terminal(_mutated(tmp_path, f))
    assert "A" in str(e.value) and "B" in str(e.value) and "overlap" in str(e.value)


def test_missing_transfer_zone_rejected(tmp_path):
    with pytest.raises(LayoutError, match="no transfer zone"):
        load_terminal(_mutated(tmp_path, lambda r: r["transfer_zones"].clear()))


def test_transfer_zone_without_block_rejected(tmp_path):
    def f(raw):
        raw["transfer_zones"].append({"block": "Z", "approach_lane_id": "DL1", "racks": []})
    with pytest.raises(LayoutError, match="unknown block"):
        load_terminal(_mutated(tmp_path, f))


def test_rack_outside_zone_rejected(tmp_path):
    def f(raw):
        raw["transfer_zones"][0]["racks"][0]["y_m"] = 170  # inside the block, not in front
        raw["transfer_zones"][0]["racks"][1]["x_m"] = 500  # far outside the block x-range
    with pytest.raises(LayoutError) as e:
        load_terminal(_mutated(tmp_path, f))
    assert "RK-A-1" in str(e.value) and "RK-A-2" in str(e.value)


def test_unknown_zone_type_rejected(tmp_path):
    with pytest.raises(LayoutError, match="unknown zone type 'nope'"):
        load_terminal(_mutated(tmp_path, lambda r: r["zones"][0].update(type="nope")))


def test_duplicate_ids_rejected(tmp_path):
    def f(raw):
        raw["sts"][0]["lanes"][1]["id"] = "STS-01-L1"
        raw["transfer_zones"][0]["racks"][1]["id"] = "RK-A-1"
        raw["zones"].append(dict(raw["zones"][0]))
    with pytest.raises(LayoutError) as e:
        load_terminal(_mutated(tmp_path, f))
    msg = str(e.value)
    assert "STS-01-L1" in msg and "RK-A-1" in msg and "OOG" in msg
    assert len(msg.strip().splitlines()) >= 3  # one line per violation


def test_collects_every_violation(tmp_path):
    def f(raw):
        raw["sts"][0]["min_gap_m"] = 10
        raw["sts"][0]["berth_ids"] = ["X"]
        raw["blocks"][0]["asc_min_gap_bays"] = 0
        raw["blocks"][0]["reefer_rows"] = [10]
        raw["quay"]["berths"][0]["to_m"] = 150
        raw["sts"][0]["lanes"] = []
        raw["sts_rails"]["y_ws_m"] = 500
    with pytest.raises(LayoutError) as e:
        load_terminal(_mutated(tmp_path, f))
    assert len(str(e.value).strip().splitlines()) >= 7


def test_optional_sections_default_empty(tmp_path):
    def f(raw):
        for k in ("landside", "zones", "opcua"):
            raw.pop(k, None)
    t = load_terminal(_mutated(tmp_path, f))
    assert t.landside == () and t.zones == ()
    assert t.opcua.base_port == 48400 and t.opcua.host == "localhost"
    assert t.depth_m == pytest.approx(165 + 260.0)


def test_is_v2_false_for_legacy():
    legacy = yaml.safe_load((Path(__file__).parents[2] / "terminal.yaml").read_text())
    assert is_v2(legacy) is False
    assert is_v2({"version": 2}) is True
