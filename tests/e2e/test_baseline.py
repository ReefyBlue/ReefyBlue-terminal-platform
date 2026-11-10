from __future__ import annotations

from pathlib import Path

import pytest
from terminal_runner.run import run_once
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.e2e
def test_baseline_200_containers_complete():
    k = run_once(load_scenario(ROOT / "scenarios/baseline.yaml"), load_layout(ROOT / "terminal.yaml"), seed=1)
    assert k["timed_out"] == 0.0
    assert k["containers_done"] == 200 and k["containers_failed"] == 0
    assert 20 <= k["moves_per_hour_STS-01"] <= 45  # plausibiliteit, geen exacte eis


@pytest.mark.e2e
def test_baseline_is_reproducible():
    scenario = load_scenario(ROOT / "scenarios/baseline.yaml")
    layout = load_layout(ROOT / "terminal.yaml")
    assert run_once(scenario, layout, seed=1) == run_once(scenario, layout, seed=1)
