"""Faults scenario over several seeds: the runner must not stop while orders wait
behind faulted equipment (no running job, not pending, but still open)."""

from __future__ import annotations

from pathlib import Path

import pytest
from terminal_runner.run import run_once
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
SEEDS = range(1, 11)


@pytest.fixture(scope="module")
def runs() -> dict[int, dict[str, float]]:
    scenario = load_scenario(ROOT / "scenarios/faults.yaml")
    layout = load_layout(ROOT / "terminal.yaml")
    return {seed: run_once(scenario, layout, seed) for seed in SEEDS}


@pytest.mark.e2e
@pytest.mark.parametrize("seed", SEEDS)
def test_faults_every_order_settles(runs, seed):
    k = runs[seed]
    assert k["timed_out"] == 0.0
    assert k["containers_done"] + k["containers_failed"] == 200
