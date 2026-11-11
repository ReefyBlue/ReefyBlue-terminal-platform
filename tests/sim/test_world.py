from pathlib import Path

import pytest
from terminal_model.locations import parse_location
from terminal_model.port import EquipmentPort, EventKind
from terminal_model.types import Job, JobType, Position, State
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_sim.world import make_world


@pytest.fixture
def world():
    return make_world(
        load_layout(Path("terminal.yaml")),
        load_scenario(Path("scenarios/baseline.yaml")),
    )


def _transport(to_loc):
    return Job("J1", JobType.TRANSPORT, "C1", "STS-01", to_loc)


def test_world_builds_from_baseline(world):
    port = world.port
    assert isinstance(port, EquipmentPort)
    assert len(port.equipment_ids()) == 12
    assert all(port.snapshot(i).state == State.IDLE for i in port.equipment_ids())
    assert port.now() == 0


def test_port_assign_and_event_roundtrip(world):
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


def test_transfer_sts_to_agv_and_agv_to_tp(world):
    assert world.transfer_sts_to_agv("STS-01", "C1") is None  # no AGV at the lane yet
    world.port.assign_job("AGV-01", _transport("STS-01"))
    world.run(300)
    assert world.equipment["AGV-01"].cell in world.layout.sts[0].lane_cells
    assert world.transfer_sts_to_agv("STS-01", "C1") == "AGV-01"
    assert world.port.snapshot("AGV-01").extra["loaded_container_id"] == "C1"
    assert world.transfer_sts_to_agv("STS-01", "C2") is None  # already loaded
    assert world.transfer_agv_to_tp("AGV-01") == "C1"


def test_transfer_sts_to_agv_picks_the_agv_sent_for_the_container(world):
    # Both lane cells hold an empty AGV; the box goes to the one sent for it,
    # not to whichever AGV comes first in equipment order.
    world.port.assign_job("AGV-01", Job("J1", JobType.TRANSPORT, "C2", "AGV-01", "STS-01"))
    world.port.assign_job("AGV-02", Job("J2", JobType.TRANSPORT, "C1", "AGV-02", "STS-01"))
    world.run(300)
    lane = world.layout.sts[0].lane_cells
    assert world.equipment["AGV-01"].cell in lane
    assert world.equipment["AGV-02"].cell in lane
    assert world.transfer_sts_to_agv("STS-01", "C1") == "AGV-02"
    assert world.transfer_sts_to_agv("STS-01", "C2") == "AGV-01"
    assert world.transfer_sts_to_agv("STS-01", "C3") is None  # no AGV was sent for C3


def test_idle_agv_parks_off_the_tp(world):
    assert world.handover_cells == {(4, 0), (5, 0), (12, 0), (13, 0), (2, 5), (7, 5), (12, 5), (17, 5)}
    assert all(y in (2, 3) for _, y in world.staging_cells)
    assert not world.staging_cells & world.handover_cells
    agv = world.equipment["AGV-01"]
    agv.load("C1")
    assert world.port.assign_job("AGV-01", _transport("STS-01"))
    world.run(100)
    assert agv.cell in world.layout.sts[0].lane_cells and agv.state == State.IDLE  # waits: box on board
    assert agv.unload() == "C1"  # taken by the crane
    world.run(200)
    assert agv.state == State.IDLE and agv.cell in world.staging_cells
    # At a TP the sim hands the box over on arrival; the empty AGV then parks.
    assert world.port.assign_job("AGV-02", Job("J2", JobType.TRANSPORT, "C2", "STS-02", "TP/A/WS"))
    world.equipment["AGV-02"].load("C2")
    world.run(500)
    assert world.handover.boxes("A") == ["C2"]
    assert world.equipment["AGV-02"].cell in world.staging_cells
    assert world.grid.owner_of((2, 5)) is None


def test_transfer_tp_to_agv_frees_the_ws_tp(world):
    asc = world.equipment["ASC-A"]
    world.yard.occupy(parse_location("A/1/1/1"), "C9")
    assert world.port.assign_job("ASC-A", Job("S1", JobType.STACK_OUT, "C9", "A/1/1/1", "TP/A/WS"))
    world.port.assign_job("AGV-01", _transport("TP/A/WS"))
    world.run(300)
    assert world.port.snapshot("ASC-A").extra["ws_tp_occupied"] is True
    assert world.equipment["AGV-01"].cell == asc.block.tp_ws_cell
    world.transfer_tp_to_agv("AGV-01", "C9")
    assert world.port.snapshot("AGV-01").extra["loaded_container_id"] == "C9"
    assert world.port.snapshot("ASC-A").extra["ws_tp_occupied"] is False
