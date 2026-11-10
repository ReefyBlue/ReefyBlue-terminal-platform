"""Mixed (50/50) and load-only traffic under faults: no lane deadlock, every order settles.

The scenario is faults.yaml with ``discharge_share`` replaced; full 200 containers.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from terminal_runner.run import run_once
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
SEEDS = range(1, 6)
CASES = [(0.5, n_agv, seed) for n_agv in (2, 4, 6) for seed in SEEDS] + [(0.0, 4, seed) for seed in SEEDS]


@pytest.fixture(scope="module")
def layout():
    return load_layout(ROOT / "terminal.yaml")


@pytest.fixture(scope="module")
def faults():
    return load_scenario(ROOT / "scenarios/faults.yaml")


@pytest.mark.e2e
@pytest.mark.parametrize(("discharge_share", "n_agv", "seed"), CASES)
def test_every_order_settles_without_deadlock(faults, layout, discharge_share, n_agv, seed):
    scenario = replace(faults, name="mixed-faults", discharge_share=discharge_share, n_agv=n_agv)
    k = run_once(scenario, layout, seed)
    assert k["timed_out"] == 0.0
    assert k["containers_done"] + k["containers_failed"] == 200
    assert k["containers_stuck"] == 0.0
