"""Baseline scenario on the v2 small layout (``terminal_mv_small.yaml``, via its 20 m grid view).

The scenario is ``scenarios/baseline.yaml`` with 3 STS and 12 AGVs (one per buffer); it is
built with ``dataclasses.replace`` because ``load_scenario`` still caps n_sts/n_agv at the
legacy 2/6. The first run writes the fixture; later runs must match it exactly.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from terminal_runner.run import run_once
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "mv_small_baseline_seed1.json"


def _run() -> dict[str, float]:
    scenario = replace(load_scenario(ROOT / "scenarios/baseline.yaml"), n_sts=3, n_agv=12)
    return run_once(scenario, load_layout(ROOT / "terminal_mv_small.yaml"), seed=1)


@pytest.mark.e2e
def test_mv_small_baseline_matches_fixture():
    k = _run()
    assert k["timed_out"] == 0.0
    assert k["containers_done"] == 200 and k["containers_failed"] == 0
    assert k["containers_stuck"] == 0
    if not FIXTURE.exists():
        FIXTURE.write_text(json.dumps(k, indent=2) + "\n")
    assert k == json.loads(FIXTURE.read_text())
