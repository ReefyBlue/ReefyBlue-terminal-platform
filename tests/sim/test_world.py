from dataclasses import replace
from pathlib import Path

import pytest
from terminal_model.port import EquipmentPort, EventKind
from terminal_model.types import Job, JobType, Position, State
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
def solo_world():
    # AGV-01 starts boxed in by AGV-02 on the one-way row 2 with the full fleet,
    # so route tests use a single-AGV scenario.
    return World(
        load_layout(Path("terminal.yaml")),
        replace(load_scenario(Path("scenarios/baseline.yaml")), n_agv=1),
    )


def _transport(to_loc):
    return Job("J1", JobType.TRANSPORT, "C1", "STS-01", to_loc)


def test_world_builds_from_baseline(world):
    port = world.port
    assert isinstance(port, EquipmentPort)
    assert len(port.equipment_ids()) == 12
    assert all(port.snapshot(i).state == State.IDLE for i in port.equipment_ids())
    assert port.now() == 0


def test_port_assign_and_event_roundtrip(solo_world):
    world = solo_world
    events = []
    world.port.subscribe(events.append)
    assert world.port.assign_job("AGV-01", _transport("TP/A/WS"))
    world.run(300)
    assert any(
        e.kind == EventKind.JOB_COMPLETED and e.equipment_id == "AGV-01" for e in events
    )
    assert world.port.snapshot("AGV-01").position == Position(50.0, 110.0)
    assert world.now() == 300


def test_port_unknown_id_raises(world):
    port = world.port
    job = _transport("TP/A/WS")
    for call in (
        lambda: port.snapshot("NOPE"),
        lambda: port.assign_job("NOPE", job),
        lambda: port.cancel_job("NOPE"),
        lambda: port.reset("NOPE"),
    ):
        with pytest.raises(KeyError):
            call()


def test_resolve_cell(world):
    assert world.resolve_cell("TP/B/WS") == (7, 5)
    assert world.resolve_cell("STS-01") == (4, 0)
    with pytest.raises(ValueError):
        world.resolve_cell("NOWHERE")


def test_transfer_sts_to_agv_and_agv_to_tp(solo_world):
    world = solo_world
    assert world.transfer_sts_to_agv("STS-01", "C1") is None  # no AGV at the lane yet
    world.port.assign_job("AGV-01", _transport("STS-01"))
    world.run(300)
    assert world.equipment["AGV-01"].cell in world.layout.sts[0].lane_cells
    assert world.transfer_sts_to_agv("STS-01", "C1") == "AGV-01"
    assert world.port.snapshot("AGV-01").extra["loaded_container_id"] == "C1"
    assert world.transfer_sts_to_agv("STS-01", "C2") is None  # already loaded
    assert world.transfer_agv_to_tp("AGV-01") == "C1"
