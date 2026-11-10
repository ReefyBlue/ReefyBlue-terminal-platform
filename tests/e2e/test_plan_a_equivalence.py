"""The sim-side box handover reproduces the Plan A (ECS ``on_transfer`` hook) baseline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from terminal_runner.run import run_once
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "plan_a_baseline_seed1.json"
NEW_DEFINITION = {"agv_wait_tp_s_mean"}  # now from job_log, no transfer hook


@pytest.mark.e2e
def test_baseline_matches_plan_a_reference():
    ref = json.loads(FIXTURE.read_text())
    k = run_once(load_scenario(ROOT / "scenarios/baseline.yaml"), load_layout(ROOT / "terminal.yaml"), 1)
    assert list(k) == list(ref)
    assert {key: k[key] for key in ref if key not in NEW_DEFINITION} == {
        key: v for key, v in ref.items() if key not in NEW_DEFINITION
    }
    assert k["agv_wait_tp_s_mean"] >= 0
