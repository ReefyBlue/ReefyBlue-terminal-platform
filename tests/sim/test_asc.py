import random
from pathlib import Path

import pytest
import salabim
from terminal_model.locations import SlotLoc
from terminal_model.port import EventKind
from terminal_model.types import Job, JobStatus, JobType, State
from terminal_sim.equipment.asc import (
    ASC,
    BAY_PITCH_M,
    ENERGY_PER_MOVE_KWH,
    GANTRY_MPS,
    HOIST_MPS,
    ROW_PITCH_M,
    TIER_H_M,
    TROLLEY_MPS,
)
from terminal_sim.layout import load_layout
from terminal_sim.yard import Yard


class Rig:
    def __init__(self):
        layout = load_layout(Path("terminal.yaml"))
        self.block = layout.blocks[0]
        assert self.block.block == "A"
        self.env = salabim.Environment(trace=False, yieldless=False)
        self.yard = Yard(layout.blocks)
        self.events = []
        self.asc = ASC(
            equipment_id=self.block.asc_id,
            position=self.block.tp_ws,
            rng=random.Random(1),
            emit=self.events.append,
            env=self.env,
            block=self.block,
            yard=self.yard,
        )


@pytest.fixture
def rig():
    return Rig()


def stack_in(slot="A/10/3/1", cid="C1", job_id="J1"):
    return Job(job_id, JobType.STACK_IN, cid, "TP/A/WS", slot)


def stack_out(slot="A/10/3/1", cid="C1", job_id="J2"):
    return Job(job_id, JobType.STACK_OUT, cid, slot, "TP/A/WS")


def of_kind(rig, kind):
    return [e for e in rig.events if e.kind == kind]


def test_stack_in_occupies_slot_and_takes_time(rig):
    # Gantry starts at bay 1; TP = bay 0, row 1, tier 1; slot A/10/3/1.
    leg_to_tp = 1 * BAY_PITCH_M / GANTRY_MPS
    hoist_tp = 2 * 1 * TIER_H_M / HOIST_MPS
    leg_tp_to_slot = 10 * BAY_PITCH_M / GANTRY_MPS + 2 * ROW_PITCH_M / TROLLEY_MPS
    hoist_slot = 2 * 1 * TIER_H_M / HOIST_MPS
    expected = leg_to_tp + hoist_tp + leg_tp_to_slot + hoist_slot

    j = stack_in()
    assert rig.asc.assign_job(j)
    rig.env.run(till=300)
    done = of_kind(rig, EventKind.JOB_COMPLETED)
    assert len(done) == 1
    assert done[0].sim_time == pytest.approx(expected, abs=1.0)
    assert rig.yard.locate("C1") == SlotLoc("A", 10, 3, 1)
    assert j.status == JobStatus.DONE
    assert rig.asc.state == State.IDLE
    assert rig.asc.energy_kwh == ENERGY_PER_MOVE_KWH
    assert rig.asc.gantry_bay == 10.0
    assert rig.asc.extra()["gantry_position_m"] == 10 * BAY_PITCH_M


def test_stack_in_occupied_slot_fails_immediately(rig):
    rig.yard.occupy(SlotLoc("A", 10, 3, 1), "OTHER")
    assert rig.asc.assign_job(stack_in())
    assert rig.env.now() == 0
    rig.env.run(till=300)
    failed = of_kind(rig, EventKind.JOB_FAILED)
    assert len(failed) == 1
    assert failed[0].reason == "SLOT_OCCUPIED"
    assert failed[0].sim_time == 0
    assert rig.yard.locate("C1") is None
    assert rig.asc.state == State.IDLE
    assert rig.asc.energy_kwh == 0.0


def test_stack_out_empty_fails(rig):
    assert rig.asc.assign_job(stack_out())
    rig.env.run(till=300)
    failed = of_kind(rig, EventKind.JOB_FAILED)
    assert len(failed) == 1
    assert failed[0].reason == "SLOT_EMPTY"
    assert failed[0].sim_time == 0
    assert rig.asc.state == State.IDLE


def test_stack_out_releases_slot(rig):
    slot = SlotLoc("A", 10, 3, 1)
    rig.yard.occupy(slot, "C1")
    j = stack_out()
    assert rig.asc.assign_job(j)
    rig.env.run(till=300)
    assert j.status == JobStatus.DONE
    assert rig.yard.locate("C1") is None
    assert rig.asc.gantry_bay == 0.0
    assert rig.asc.energy_kwh == ENERGY_PER_MOVE_KWH


def test_rejects_transport_job(rig):
    j = Job("J9", JobType.TRANSPORT, "C1", "TP/A/WS", "A/10/3/1")
    assert rig.asc.assign_job(j) is False
    assert rig.asc.state == State.IDLE


def test_rejects_wrong_block_or_bad_location(rig):
    assert rig.asc.assign_job(stack_in(slot="B/1/1/1")) is False
    assert (
        rig.asc.assign_job(Job("J3", JobType.STACK_IN, "C1", "TP/A/LS", "A/1/1/1"))
        is False
    )
    assert (
        rig.asc.assign_job(Job("J4", JobType.STACK_IN, "C1", "nonsense", "A/1/1/1"))
        is False
    )


def test_ws_tp_occupied_from_stack_in_assignment_to_completion(rig):
    assert rig.asc.extra()["ws_tp_occupied"] is False
    assert rig.asc.assign_job(stack_in())
    assert rig.asc.extra()["ws_tp_occupied"] is True  # the AGV has put the box down
    rig.env.run(till=300)
    assert of_kind(rig, EventKind.JOB_COMPLETED)
    assert rig.asc.extra()["ws_tp_occupied"] is False


def test_ws_tp_occupied_after_stack_out(rig):
    rig.yard.occupy(SlotLoc("A", 10, 3, 1), "C1")
    assert rig.asc.assign_job(stack_out())
    assert rig.asc.extra()["ws_tp_occupied"] is False
    rig.env.run(till=300)
    assert rig.asc.extra()["ws_tp_occupied"] is True  # box waits on the TP for an AGV
