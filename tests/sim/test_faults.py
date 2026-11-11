from dataclasses import replace
from itertools import pairwise
from pathlib import Path

from terminal_model.port import EventKind
from terminal_model.types import Job, JobType, Kind, State
from terminal_sim.layout import load_layout
from terminal_sim.scenario import FaultParams, load_scenario
from terminal_sim.world import make_world


def _world(agv: FaultParams):
    scenario = load_scenario(Path("scenarios/baseline.yaml"))
    scenario = replace(
        scenario,
        faults={
            Kind.AGV: agv,
            Kind.STS: FaultParams(0, 0),
            Kind.ASC: FaultParams(0, 0),
        },
    )
    return make_world(load_layout(Path("terminal.yaml")), scenario)


def test_fault_cycle():
    world = _world(FaultParams(100, 10))
    events = []
    world.port.subscribe(events.append)
    world.run(1000)
    injector = next(i for i in world.fault_injectors if i.target.equipment_id == "AGV-01")
    assert injector.count >= 5
    states = [
        e.new_state
        for e in events
        if e.kind == EventKind.STATE_CHANGED and e.equipment_id == "AGV-01"
    ]
    assert states[0] == State.FAULT
    assert all(a != b for a, b in pairwise(states))
    assert all(s in (State.FAULT, State.IDLE) for s in states)
    assert world.faults_total() >= 5


def test_fault_fails_running_job():
    world = _world(FaultParams(50, 10))
    events = []
    world.port.subscribe(events.append)
    job = Job("J1", JobType.TRANSPORT, "C1", "STS-01", "TP/A/WS")
    assert world.port.assign_job("AGV-01", job)
    while not any(e.kind == EventKind.JOB_FAILED for e in events) and world.now() < 5000:
        world.run(world.now() + 10)
    failed = [e for e in events if e.kind == EventKind.JOB_FAILED]
    assert failed
    assert failed[0].reason == "FAULT"
    assert failed[0].job_id == "J1"
    # FAULT is set first: no transient IDLE between the FAULT and JOB_FAILED events
    agv = [e for e in events if e.equipment_id == "AGV-01"]
    i_fail = agv.index(failed[0])
    i_fault = max(
        i
        for i, e in enumerate(agv[:i_fail])
        if e.kind == EventKind.STATE_CHANGED and e.new_state == State.FAULT
    )
    between = agv[i_fault + 1 : i_fail]
    assert not any(
        e.kind == EventKind.STATE_CHANGED and e.new_state == State.IDLE for e in between
    )
    assert world.port.snapshot("AGV-01").state == State.FAULT


def test_no_injector_when_mtbf_zero():
    world = make_world(
        load_layout(Path("terminal.yaml")),
        load_scenario(Path("scenarios/baseline.yaml")),
    )
    assert world.fault_injectors == []
    assert world.faults_total() == 0


def test_injector_rejects_non_positive_params():
    import random

    import pytest
    from terminal_sim.faults import FaultInjector

    world = _world(FaultParams(0, 0))
    eq = world.equipment["AGV-01"]
    for bad in (FaultParams(0, 10), FaultParams(10, 0)):
        with pytest.raises(ValueError):
            FaultInjector(target=eq, params=bad, rng=random.Random(1), env=world.env)
