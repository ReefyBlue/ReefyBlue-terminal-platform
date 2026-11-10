"""OPC UA (sim + gateway processes) vs the in-process run.

Counts must match exactly. Timing KPIs carry a structural lag: every handoff
event -> gateway -> runner -> AssignJob costs 50-100 ms wall, i.e. 5-10 s sim at
speed_factor 100, so the tolerances are wider at x100 and tighter at x25."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from opcua_procs import ROOT, sim_and_gateway
from terminal_runner.run import run_once, run_once_opcua
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

log = logging.getLogger(__name__)
COUNTS = ("containers_done", "containers_failed", "replans", "faults")


@pytest.mark.e2e_opcua
def test_baseline_opcua_matches_inprocess(tmp_path):
    text = (ROOT / "scenarios/baseline.yaml").read_text().replace("speed_factor: 50", "speed_factor: 100")
    assert "speed_factor: 100" in text
    path = Path(tmp_path / "baseline.yaml")
    path.write_text(text)
    scenario = load_scenario(path)
    layout = load_layout(ROOT / "terminal.yaml")
    inproc = run_once(scenario, layout, 1)
    with sim_and_gateway(path, n_sts=2, n_agv=6, log_dir=tmp_path) as procs:
        opcua = run_once_opcua(scenario, layout, 1, gateway=procs["endpoint"])
    table = {key: (inproc[key], opcua[key]) for key in inproc}
    log.info("in-process vs OPC UA:\n%s", json.dumps(table, indent=1))
    print(json.dumps(table, indent=1))
    assert procs["returncodes"] == (0, 0), procs["logs"]
    assert {c: opcua[c] for c in COUNTS} == {c: inproc[c] for c in COUNTS}, table
    assert opcua["moves_per_hour_total"] == pytest.approx(inproc["moves_per_hour_total"], rel=0.10), table
    assert abs(opcua["vessel_turnaround_s"] - inproc["vessel_turnaround_s"]) <= 900, table
    assert opcua["timed_out"] == inproc["timed_out"] == 0, table


SMALL_X25 = """\
name: small_x25
seed: 7
n_sts: 1
n_agv: 2
n_containers: 20
share_40ft: 0.5
discharge_share: 1.0
speed_factor: 25
duration_s: 7200
"""


@pytest.mark.e2e_opcua
def test_small_scenario_gap_shrinks_with_lower_speed(tmp_path):
    path = tmp_path / "small_x25.yaml"
    path.write_text(SMALL_X25)
    scenario = load_scenario(path)
    layout = load_layout(ROOT / "terminal.yaml")
    inproc = run_once(scenario, layout, scenario.seed)
    with sim_and_gateway(path, n_sts=1, n_agv=2, log_dir=tmp_path) as procs:
        opcua = run_once_opcua(scenario, layout, scenario.seed, gateway=procs["endpoint"])
    table = {key: (inproc[key], opcua[key]) for key in inproc}
    log.info("in-process vs OPC UA (x25):\n%s", json.dumps(table, indent=1))
    print(json.dumps(table, indent=1))
    assert procs["returncodes"] == (0, 0), procs["logs"]
    assert {c: opcua[c] for c in COUNTS} == {c: inproc[c] for c in COUNTS}, table
    assert opcua["moves_per_hour_total"] == pytest.approx(inproc["moves_per_hour_total"], rel=0.05), table
    assert abs(opcua["vessel_turnaround_s"] - inproc["vessel_turnaround_s"]) <= 120, table
    assert opcua["timed_out"] == inproc["timed_out"] == 0, table
