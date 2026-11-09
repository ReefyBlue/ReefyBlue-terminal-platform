from pathlib import Path

import pytest
import yaml

from terminal_model.types import Kind
from terminal_sim.scenario import load_scenario

BASE = dict(
    name="t", seed=1, n_sts=2, n_agv=6, n_containers=200, share_40ft=0.6,
    discharge_share=1.0, speed_factor=50, duration_s=43200,
)


def test_baseline_loads():
    s = load_scenario(Path("scenarios/baseline.yaml"))
    assert (s.seed, s.n_sts, s.n_agv, s.n_containers) == (1, 2, 6, 200)
    assert s.faults[Kind.AGV].mtbf_s == 0
    assert set(s.faults) == set(Kind)


def test_agv4_and_faults_scenarios():
    assert load_scenario(Path("scenarios/agv4.yaml")).n_agv == 4
    f = load_scenario(Path("scenarios/faults.yaml"))
    assert (f.faults[Kind.STS].mtbf_s, f.faults[Kind.STS].mttr_s) == (7200, 600)
    assert (f.faults[Kind.AGV].mtbf_s, f.faults[Kind.AGV].mttr_s) == (14400, 300)
    assert (f.faults[Kind.ASC].mtbf_s, f.faults[Kind.ASC].mttr_s) == (10800, 900)


@pytest.mark.parametrize(
    "field,value",
    [
        ("n_agv", 0), ("n_sts", 0), ("n_agv", 7), ("n_sts", 3),
        ("n_containers", 0), ("share_40ft", 1.5), ("share_40ft", -0.1),
    ],
)
def test_scenario_rejects(tmp_path, field, value):
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump({**BASE, field: value}))
    with pytest.raises(ValueError, match=field):
        load_scenario(p)
