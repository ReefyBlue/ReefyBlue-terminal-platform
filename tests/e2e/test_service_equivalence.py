"""The ECS service loop reproduces the in-process runner: an ``EcsCore`` on ``World.port``
behind ``create_app``, driven per 60 s of sim time, yields the Plan A baseline KPIs."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from terminal_ecs_service.app import create_app
from terminal_ecs_service.core import EcsCore
from terminal_runner.run import make_allocator, make_workorders, prefill
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_sim.world import World

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "plan_a_baseline_seed1.json"
NEW_DEFINITION = {"agv_wait_tp_s_mean"}  # now from job_log, no transfer hook
RUNNER_ONLY = {"timed_out"}  # set by the runner, not by the service


@pytest.mark.e2e
def test_service_loop_matches_plan_a_reference():
    ref = json.loads(FIXTURE.read_text())
    scenario = load_scenario(ROOT / "scenarios/baseline.yaml")
    layout = load_layout(ROOT / "terminal.yaml")
    assert scenario.seed == 1
    world = World(layout, scenario)
    orders = make_workorders(scenario, layout, make_allocator(layout), random.Random(1))
    core = EcsCore(layout, connect=lambda: world.port)
    app = create_app(core, run_loop=False)

    with TestClient(app) as client:
        assert client.portal.call(core.connect_once) is True
        prefill(world.yard, orders)
        body = [{"container_id": o.container_id, "type": o.type.value,
                 "sts_id": o.sts_id, "yard_slot": o.yard_slot} for o in orders]
        r = client.post("/workorders", json=body)
        assert r.status_code == 202 and r.json() == {"accepted": len(orders)}
        t = 0.0
        while t < scenario.duration_s:
            t = min(t + 60.0, scenario.duration_s)
            world.run(until=t)
            client.portal.call(core.step)
            if client.get("/status").json()["open"] == 0:
                break
        status = client.get("/status").json()
        k = client.get("/kpis").json()

    assert status["open"] == 0  # replaces the runner's timed_out == 0.0
    keys = [key for key in ref if key not in RUNNER_ONLY]
    assert list(k) == keys
    compared = [key for key in keys if key not in NEW_DEFINITION]
    assert {key: k[key] for key in compared} == {key: ref[key] for key in compared}
    assert k["agv_wait_tp_s_mean"] >= 0
