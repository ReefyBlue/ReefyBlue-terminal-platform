"""Baseline scenario on the v2 small layout (``terminal_mv_small.yaml``, via its 20 m grid view).

The scenario is ``fixtures/mv_small_baseline.yaml``: ``scenarios/baseline.yaml`` with 3 STS
and 12 AGVs (one per buffer), capped by the layout. Runs must match the committed fixture exactly;
it is (re)written only with ``WRITE_FIXTURES=1``, never silently.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from terminal_runner.run import run_once
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "mv_small_baseline_seed1.json"
SCENARIO = Path(__file__).parent / "fixtures" / "mv_small_baseline.yaml"


def _run() -> dict[str, float]:
    layout = load_layout(ROOT / "terminal_mv_small.yaml")
    return run_once(load_scenario(SCENARIO, layout), layout, seed=1)


@pytest.mark.e2e
@pytest.mark.xfail(strict=True, reason="C1b Task 4/7: the v2 World2 needs the rack-mode dispatcher")
def test_mv_small_baseline_matches_fixture():
    k = _run()
    assert k["timed_out"] == 0.0
    assert k["containers_done"] == 200 and k["containers_failed"] == 0
    assert k["containers_stuck"] == 0
    if os.environ.get("WRITE_FIXTURES") == "1":
        FIXTURE.write_text(json.dumps(k, indent=2) + "\n")
    assert FIXTURE.exists(), (
        f"missing fixture {FIXTURE.name}; regenerate deliberately with WRITE_FIXTURES=1")
    assert k == json.loads(FIXTURE.read_text())
