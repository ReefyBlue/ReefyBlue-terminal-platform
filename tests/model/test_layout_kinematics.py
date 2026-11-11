from pathlib import Path

import pytest
import yaml
from terminal_model.layout import LayoutError, load_terminal
from terminal_model.layout_builder import to_yaml_dict



SHIPPED = Path(__file__).resolve().parents[2] / "terminal_mv_small.yaml"


def _load(tmp_path, kin):
    raw = yaml.safe_load(SHIPPED.read_text())
    if kin is not None:
        raw["kinematics"] = kin
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_terminal(p)


def test_default_empty(tmp_path):
    assert _load(tmp_path, None).kinematics == {}


def test_valid_kinds_and_ids(tmp_path):
    t = _load(tmp_path, {"AGV": {"straight.v_max": 5}, "AGV-07": {"rack_lift_s": 10},
                         "STS-01": {"trolley.accel": 1.0}, "ASC-A-WS": {"spreader_lock_s": 5}})
    assert t.kinematics["AGV"] == {"straight.v_max": 5.0}
    assert t.kinematics["AGV-07"]["rack_lift_s"] == 10.0


@pytest.mark.parametrize("kin, msg", [
    ({"CRANE": {"a.b": 1}}, "unknown kinematics key"),
    ({"AGV-x": {"length_m": 1}}, "unknown kinematics key"),
    ({"AGV": {"warp.v_max": 1}}, "unknown parameter"),
    ({"STS": {"rack_lift_s": 1}}, "unknown parameter"),
    ({"STS": {"trolley.v_max": 0}}, "> 0"),
    ({"STS": {"trolley.v_max": -1}}, "> 0"),
    ({"STS": {"trolley.v_max": "fast"}}, "numeric"),
    ({"STS": {"trolley.v_max": True}}, "numeric"),
    ({"STS": "x"}, "must be a mapping"),
])
def test_invalid_rejected(tmp_path, kin, msg):
    with pytest.raises(LayoutError, match=msg):
        _load(tmp_path, kin)


def test_all_violations_collected(tmp_path):
    with pytest.raises(LayoutError) as e:
        _load(tmp_path, {"CRANE": {"a.b": 1}, "STS": {"trolley.v_max": 0}})
    assert "CRANE" in str(e.value) and "> 0" in str(e.value)


def test_builder_omits_empty_and_roundtrips(tmp_path):
    t = _load(tmp_path, {"AGV": {"straight.v_max": 5}})
    d = to_yaml_dict(t)
    assert d["kinematics"] == {"AGV": {"straight.v_max": 5.0}}
    assert "kinematics" not in to_yaml_dict(_load(tmp_path, None))
