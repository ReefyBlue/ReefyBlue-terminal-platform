"""Sim process + gateway process + runner over OPC UA: a small mixed scenario completes
(10 LOAD containers placed via PrefillYard while the sim clock is paused)."""

from __future__ import annotations

import logging
import time

import pytest
from opcua_procs import ROOT, sim_and_gateway
from terminal_runner.run import run_once_opcua
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

SMALL = """\
name: small_opcua
seed: 7
n_sts: 1
n_agv: 2
n_containers: 20
share_40ft: 0.5
discharge_share: 0.5
speed_factor: 100
duration_s: 7200
"""

log = logging.getLogger(__name__)


@pytest.mark.e2e_opcua
def test_small_scenario_completes_over_opcua(tmp_path):
    path = tmp_path / "small.yaml"
    path.write_text(SMALL)
    scenario = load_scenario(path)
    with sim_and_gateway(path, n_sts=1, n_agv=2, log_dir=tmp_path) as procs:
        t0 = time.monotonic()
        k = run_once_opcua(scenario, load_layout(ROOT / "terminal.yaml"), scenario.seed,
                           gateway=procs["endpoint"])
        wall = time.monotonic() - t0
    log.info("small over OPC UA: %s (run %.1f s wall)", k, wall)
    assert procs["returncodes"] == (0, 0), procs["logs"]
    assert k["containers_done"] == 20, k
    assert k["containers_failed"] == 0, k
    assert k["timed_out"] == 0, k
    assert wall < 60, wall
