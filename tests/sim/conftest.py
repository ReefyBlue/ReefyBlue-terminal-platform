"""Fixtures for the v2 world (``terminal_mv_small.yaml``)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from terminal_model.port import EventKind
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_sim.world import make_world

ROOT = Path(__file__).resolve().parents[2]
MV_SMALL = ROOT / "terminal_mv_small.yaml"
MV_SCENARIO = ROOT / "tests" / "e2e" / "fixtures" / "mv_small_baseline.yaml"


@pytest.fixture
def make_world2():
    def make(**overrides):
        layout = load_layout(MV_SMALL)
        scenario = replace(load_scenario(MV_SCENARIO, layout), **overrides)
        return make_world(layout, scenario)

    return make


@pytest.fixture
def world2(make_world2):
    return make_world2()


class Recorder:
    """Collects port events; ``done(eid, job_id)`` = completion time or None."""

    def __init__(self, world) -> None:
        self.events = []
        world.port.subscribe(self.events.append)

    def done(self, eid: str, job_id: str) -> float | None:
        return next((e.sim_time for e in self.events if e.kind is EventKind.JOB_COMPLETED
                     and e.equipment_id == eid and e.job_id == job_id), None)

    def failed(self, eid: str, job_id: str) -> str | None:
        return next((e.reason for e in self.events if e.kind is EventKind.JOB_FAILED
                     and e.equipment_id == eid and e.job_id == job_id), None)


@pytest.fixture
def rec():
    return Recorder
