"""Faults scenario (seed 1) reproduces the reference captured before Plan B2d."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from terminal_runner.run import run_once
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "faults_pre_b2d_seed1.json"
KEYS = ("containers_done", "containers_failed", "replans", "faults", "moves_per_hour_total")


@pytest.mark.e2e
def test_faults_matches_pre_b2d_reference():
    ref = json.loads(FIXTURE.read_text())
    k = run_once(load_scenario(ROOT / "scenarios/faults.yaml"), load_layout(ROOT / "terminal.yaml"), 1)
    assert {key: k[key] for key in KEYS} == {key: ref[key] for key in KEYS}
