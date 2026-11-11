import math

from pathlib import Path

import pytest
import yaml
from terminal_model.layout import LayoutError, load_terminal
from terminal_sim.kinematics import (
    DEFAULTS, SAMPLE_DT, Axis, AgvKin, AscKin, StsKin, kin_for, sample_times,
)



SHIPPED = Path(__file__).resolve().parents[2] / "terminal_mv_small.yaml"


def _layout(tmp_path, kin=None):
    raw = yaml.safe_load(SHIPPED.read_text())
    if kin is not None:
        raw["kinematics"] = kin
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_terminal(p)


def test_trapezoid_time():
    ax = Axis(6.0, 1.0, 1.2)
    # 100 m: accel 6 s/18 m, decel 5 s/15 m, cruise 67 m
    assert ax.travel_time(100) == pytest.approx(6 + 5 + 67 / 6)


def test_short_move_triangular():
    ax = Axis(6.0, 1.0, 1.2)
    vp = math.sqrt(2 * 2 * 1.0 * 1.2 / 2.2)
    assert vp < 6
    assert ax.travel_time(2) == pytest.approx(vp / 1.0 + vp / 1.2)
    assert ax.travel_time(0) == 0.0


def test_position_at_monotonic():
    ax = Axis(6.0, 1.0, 1.2)
    for d in (2.0, 100.0):
        T = ax.travel_time(d)
        ps = [ax.position_at(d, T * i / 200) for i in range(201)]
        assert ps[0] == 0.0 and ps[-1] == pytest.approx(d)
        assert all(b >= a for a, b in zip(ps, ps[1:]))
        assert ax.position_at(d, -1) == 0.0 and ax.position_at(d, T + 10) == d
    assert ax.position_at(0, 1.0) == 0.0


def test_defaults_match_spec():
    s, a, c = DEFAULTS["STS"], DEFAULTS["AGV"], DEFAULTS["ASC"]
    assert isinstance(s, StsKin) and isinstance(a, AgvKin) and isinstance(c, AscKin)
    assert (s.trolley.v_max, s.trolley.accel) == (3.5, 0.8)
    assert (s.hoist_laden.v_max, s.hoist_empty.v_max, s.hoist_laden.accel) == (1.4, 2.8, 0.6)
    assert (s.gantry.v_max, s.gantry.accel, s.twistlock_s) == (0.75, 0.2, 12)
    assert (a.straight.v_max, a.turning.v_max, a.zone.v_max) == (6.0, 3.0, 1.0)
    assert (a.straight.accel, a.straight.decel) == (1.0, 1.2)
    assert (a.rack_position_s, a.rack_lift_s, a.length_m, a.width_m) == (8, 12, 14.8, 3.0)
    assert (c.gantry.v_max, c.gantry.accel, c.trolley.v_max) == (2.4, 0.5, 1.17)
    assert (c.hoist_laden.v_max, c.hoist_empty.v_max, c.hoist_laden.accel) == (0.75, 1.5, 0.4)
    assert c.spreader_lock_s == 6


def test_override_by_kind_and_id(tmp_path):
    lay = _layout(tmp_path, {"AGV": {"straight.v_max": 5}, "AGV-01": {"straight.v_max": 4}})
    assert kin_for(lay, "AGV-01", "AGV").straight.v_max == 4
    assert kin_for(lay, "AGV-02", "AGV").straight.v_max == 5
    assert kin_for(lay, "AGV-02", "AGV").turning.v_max == 3.0
    assert DEFAULTS["AGV"].straight.v_max == 6.0


def test_scalar_override(tmp_path):
    lay = _layout(tmp_path, {"STS-01": {"twistlock_s": 9}})
    assert kin_for(lay, "STS-01", "STS").twistlock_s == 9
    assert kin_for(lay, "STS-02", "STS").twistlock_s == 12


def test_unknown_kinematics_key_rejected(tmp_path):
    with pytest.raises(LayoutError):
        _layout(tmp_path, {"BOGUS": {"x.y": 1}})


def test_sample_times():
    assert SAMPLE_DT == 0.5
    assert sample_times(1.2) == [0.5, 1.0, 1.2]
    assert sample_times(1.0) == [0.5, 1.0]
    assert sample_times(0) == []
