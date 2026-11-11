"""Sim + gateway + ECS service on the v2 small layout (``terminal_mv_small.yaml``); the runner
drives ``fixtures/mv_small_baseline.yaml`` (3 STS, 12 AGVs, 200 containers) over HTTP.

19 devices (3 STS + 12 AGV + 4 ASC): the clock server sits on ``base_port + 19``."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
import pytest
from opcua_procs import ROOT, ecs_service, sim_and_gateway
from terminal_runner.run import run_once_http
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

LAYOUT = ROOT / "terminal_mv_small.yaml"
SCENARIO = Path(__file__).parent / "fixtures" / "mv_small_baseline.yaml"

log = logging.getLogger(__name__)


@pytest.mark.e2e_opcua
def test_mv_small_baseline_over_opcua(tmp_path):
    layout = load_layout(LAYOUT)
    scenario = load_scenario(SCENARIO, layout)
    with sim_and_gateway(SCENARIO, n_sts=scenario.n_sts, n_agv=scenario.n_agv, log_dir=tmp_path,
                         layout=LAYOUT) as procs, \
            ecs_service(procs["endpoint"], log_dir=tmp_path, layout=LAYOUT) as ecs:
        served = httpx.get(f"{ecs['url']}/layout", timeout=5.0).json()
        t0 = time.monotonic()
        k = run_once_http(scenario, layout, scenario.seed, ecs=ecs["url"], gateway=procs["endpoint"])
        wall = time.monotonic() - t0
    log.info("mv_small over OPC UA: %s (run %.1f s wall)", k, wall)
    assert procs["returncodes"] == (0, 0), procs["logs"]
    assert ecs["returncode"] == 0, ecs["log"]
    assert served["version"] == 2
    assert len(served["terminal"]["blocks"]) == 4
    assert k["containers_done"] == 200, k
    assert k["containers_failed"] == 0, k
    assert k["timed_out"] == 0, k
