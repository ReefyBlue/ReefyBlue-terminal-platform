import pytest

from terminal_model.types import Container, Job, JobStatus, JobType, Kind, Position, State


def test_container_rejects_invalid_length():
    with pytest.raises(ValueError):
        Container("C1", 45, 20.0, "A")


@pytest.mark.parametrize("length", [20, 40])
def test_container_accepts_valid_length(length):
    assert Container("C1", length, 20.0, "A").length_ft == length


def test_position_distance():
    assert Position(0, 0).distance_to(Position(3, 4)) == 5.0


def test_job_defaults():
    job = Job("J1", JobType.LOAD, "C1", "A/1/1/1", "STS-01")
    assert job.status is JobStatus.ASSIGNED
    assert job.priority == 0
    assert job.deadline is None


def test_enums_are_strings():
    assert Kind.STS == "STS"
    assert State.MAINTENANCE == "MAINTENANCE"
    assert JobType.STACK_IN == "STACK_IN"
