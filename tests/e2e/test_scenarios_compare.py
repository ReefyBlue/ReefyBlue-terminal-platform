from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from terminal_runner.run import run_once
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def layout():
    return load_layout(ROOT / "terminal.yaml")


@pytest.fixture(scope="module")
def base(layout):
    return run_once(load_scenario(ROOT / "scenarios/baseline.yaml"), layout, seed=1)


@pytest.mark.e2e
def test_fewer_agvs_lower_throughput(base, layout):
    """Throughput must not rise when AGVs are removed.

    With discharge-only traffic, 4 AGVs are barely a bottleneck (measured: 4 AGV is ~0.5 %
    slower than 6), so the 4-AGV comparison is non-strict. A 2-AGV variant of the baseline
    must be strictly slower, which proves the AGV count really drives throughput.
    """
    agv4 = run_once(load_scenario(ROOT / "scenarios/agv4.yaml"), layout, seed=1)
    assert agv4["timed_out"] == 0.0
    assert agv4["containers_done"] == 200
    assert agv4["moves_per_hour_total"] <= base["moves_per_hour_total"]

    agv2 = run_once(replace(load_scenario(ROOT / "scenarios/baseline.yaml"), name="agv2", n_agv=2), layout, seed=1)
    assert agv2["timed_out"] == 0.0
    assert agv2["containers_done"] == 200
    assert agv2["moves_per_hour_total"] < base["moves_per_hour_total"]


@pytest.mark.e2e
def test_faults_scenario_completes_with_replans(base, layout):
    k = run_once(load_scenario(ROOT / "scenarios/faults.yaml"), layout, seed=1)
    assert k["timed_out"] == 0.0
    assert k["faults"] > 0 and k["replans"] > 0
    assert k["containers_done"] == 200 and k["containers_failed"] == 0
    assert k["moves_per_hour_total"] < base["moves_per_hour_total"]
