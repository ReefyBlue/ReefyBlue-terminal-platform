import json

import pytest
from terminal_model.port import EquipmentEvent, EventKind
from terminal_model.types import Job, JobStatus, JobType, State
from terminal_opcua.codec import (
    event_from_json,
    event_to_json,
    extra_from_json,
    job_from_json,
    job_to_json,
    snapshot_extra_to_json,
)


def test_job_roundtrip():
    job = Job("J1", JobType.LOAD, "C1", "A", "B", priority=3, deadline=None, status=JobStatus.RUNNING)
    assert job_from_json(job_to_json(job)) == job
    job2 = Job("J2", JobType.STACK_IN, "C2", "A", "B", deadline=12.5)
    assert job_from_json(job_to_json(job2)) == job2
    assert json.loads(job_to_json(job))["status"] == "RUNNING"
    assert json.loads(job_to_json(job))["deadline"] is None


def test_event_roundtrip():
    k = list(EventKind)[0]
    ev = EquipmentEvent(k, "STS1", 4.5, job_id="J1", reason=None, new_state=State.FAULT)
    assert event_from_json(event_to_json(ev)) == ev
    assert json.loads(event_to_json(ev))["new_state"] == "FAULT"


def test_job_from_json_missing_field_raises():
    d = json.loads(job_to_json(Job("J1", JobType.LOAD, "C1", "A", "B")))
    del d["container_id"]
    with pytest.raises(ValueError):
        job_from_json(json.dumps(d))


def test_job_from_json_bad_enum_raises():
    d = json.loads(job_to_json(Job("J1", JobType.LOAD, "C1", "A", "B")))
    d["type"] = "NOPE"
    with pytest.raises(ValueError):
        job_from_json(json.dumps(d))


def test_event_from_json_bad_input_raises():
    with pytest.raises(ValueError):
        event_from_json("{}")


def test_extra_roundtrip_nested():
    extra = {"route": [[1, 2], [3, 4]], "battery_pct": 97.5}
    assert extra_from_json(snapshot_extra_to_json(extra)) == extra
