"""The simulator hands boxes over by itself (no ``on_transfer`` hooks)."""

from pathlib import Path

import pytest
from terminal_model.locations import parse_location
from terminal_model.port import EventKind
from terminal_model.types import Job, JobType, State
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_sim.world import World


@pytest.fixture
def world():
    return World(
        load_layout(Path("terminal.yaml")),
        load_scenario(Path("scenarios/baseline.yaml")),
    )


@pytest.fixture
def events(world):
    evs = []
    world.port.subscribe(evs.append)
    return evs


def _transport(agv, cid, to_loc, job_id="T1"):
    return Job(job_id, JobType.TRANSPORT, cid, agv, to_loc)


def _ws_occupied(world, asc="ASC-A"):
    return world.port.snapshot(asc).extra["ws_tp_occupied"]


def test_handover_is_the_first_subscriber(world):
    assert world.port._subscribers[0] == world.handover.on_event


def test_discharge_puts_box_on_sent_agv(world, events):
    assert world.port.assign_job("AGV-01", _transport("AGV-01", "C1", "STS-01"))
    assert world.port.assign_job("STS-01", Job("D1", JobType.DISCHARGE, "C1", "VESSEL", "STS-01"))
    world.run(600)
    assert world.equipment["AGV-01"].loaded_container_id == "C1"
    assert any(e.kind is EventKind.JOB_COMPLETED and e.job_id == "D1" for e in events)


def test_discharge_without_matching_agv_fails_job(world, events):
    assert world.port.assign_job("AGV-01", _transport("AGV-01", "C2", "STS-01"))
    assert world.port.assign_job("STS-01", Job("D1", JobType.DISCHARGE, "C1", "VESSEL", "STS-01"))
    world.run(600)
    failed = [e for e in events if e.kind is EventKind.JOB_FAILED and e.job_id == "D1"]
    assert [e.reason for e in failed] == ["NO_AGV_IN_LANE"]
    assert not any(e.kind is EventKind.JOB_COMPLETED and e.job_id == "D1" for e in events)
    assert world.equipment["AGV-01"].loaded_container_id is None
    assert world.equipment["STS-01"].state is State.IDLE


def test_agv_unloads_at_tp_and_asc_flag_set(world):
    agv = world.equipment["AGV-01"]
    agv.load("C1")
    assert world.port.assign_job("AGV-01", _transport("AGV-01", "C1", "TP/A/WS"))
    world.run(200)
    assert agv.loaded_container_id is None
    assert world.handover.tp_box["A"] == ["C1"]
    assert _ws_occupied(world) is True
    assert world.port.assign_job("ASC-A", Job("S1", JobType.STACK_IN, "C1", "TP/A/WS", "A/1/1/1"))
    world.run(600)
    assert world.yard.locate("C1") == parse_location("A/1/1/1")
    assert world.handover.tp_box["A"] == []
    assert _ws_occupied(world) is False


def test_stack_out_then_agv_loads_at_tp(world):
    world.yard.occupy(parse_location("A/1/1/1"), "C9")
    assert world.port.assign_job("ASC-A", Job("S1", JobType.STACK_OUT, "C9", "A/1/1/1", "TP/A/WS"))
    world.run(200)
    assert world.handover.tp_box["A"] == ["C9"]
    assert _ws_occupied(world) is True
    agv = world.equipment["AGV-01"]
    assert world.port.assign_job("AGV-01", _transport("AGV-01", "C9", "TP/A/WS"))
    world.run(600)
    assert agv.loaded_container_id == "C9"
    assert world.handover.tp_box["A"] == []
    assert _ws_occupied(world) is False


def test_agv_sent_for_another_box_does_not_take_it(world):
    world.yard.occupy(parse_location("A/1/1/1"), "C9")
    assert world.port.assign_job("ASC-A", Job("S1", JobType.STACK_OUT, "C9", "A/1/1/1", "TP/A/WS"))
    assert world.port.assign_job("AGV-01", _transport("AGV-01", "C8", "TP/A/WS"))
    world.run(600)
    assert world.equipment["AGV-01"].loaded_container_id is None
    assert world.handover.tp_box["A"] == ["C9"]
    assert _ws_occupied(world) is True


def test_second_box_on_tp_is_recorded_and_flag_stays_set(world):
    # A LOAD box waits on the TP while a discharge box is put down: the sim records
    # both (the ECS owns the capacity rule); the flag drops only once both are gone.
    world.yard.occupy(parse_location("A/1/1/1"), "L1")
    assert world.port.assign_job("ASC-A", Job("S1", JobType.STACK_OUT, "L1", "A/1/1/1", "TP/A/WS"))
    world.run(200)
    agv1 = world.equipment["AGV-01"]
    agv1.load("D1")
    assert world.port.assign_job("AGV-01", _transport("AGV-01", "D1", "TP/A/WS"))
    world.run(400)
    assert world.handover.tp_box["A"] == ["L1", "D1"]
    assert world.port.assign_job("ASC-A", Job("S2", JobType.STACK_IN, "D1", "TP/A/WS", "A/2/1/1"))
    world.run(800)
    assert world.handover.tp_box["A"] == ["L1"]
    assert _ws_occupied(world) is True
    assert world.port.assign_job("AGV-02", _transport("AGV-02", "L1", "TP/A/WS", job_id="T2"))
    world.run(1400)
    assert world.equipment["AGV-02"].loaded_container_id == "L1"
    assert world.handover.tp_box["A"] == []
    assert _ws_occupied(world) is False


def test_load_empties_agv(world, events):
    agv = world.equipment["AGV-01"]
    agv.load("L1")
    assert world.port.assign_job("AGV-01", _transport("AGV-01", "L1", "STS-01"))
    assert world.port.assign_job("STS-01", Job("LD1", JobType.LOAD, "L1", "STS-01", "VESSEL"))
    world.run(600)
    assert any(e.kind is EventKind.JOB_COMPLETED and e.job_id == "LD1" for e in events)
    assert agv.loaded_container_id is None
