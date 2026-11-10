import json

import pytest
from terminal_model.port import EquipmentEvent, EventKind
from terminal_model.types import Job, JobStatus, JobType, State
from terminal_opcua.codec import (
    event_fields,
    event_from_fields,
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


def test_event_fields_roundtrip():
    ev = EquipmentEvent(EventKind.STATE_CHANGED, "AGV-01", 12.5, new_state=State.FAULT, reason="X")
    f = event_fields(ev)
    assert f == {"EquipmentId": "AGV-01", "SimTime": 12.5, "JobId": "", "Reason": "X", "NewState": "FAULT"}
    assert event_from_fields(EventKind.STATE_CHANGED, f) == ev


def test_event_fields_roundtrip_none_fields():
    ev = EquipmentEvent(EventKind.JOB_COMPLETED, "STS-01", 1.0, job_id="J1")
    back = event_from_fields(EventKind.JOB_COMPLETED, event_fields(ev))
    assert back == ev and back.reason is None and back.new_state is None


def test_event_from_fields_missing_raises():
    with pytest.raises(ValueError):
        event_from_fields(EventKind.JOB_FAILED, {"EquipmentId": "A"})


def test_event_from_fields_bad_state_raises():
    base = event_fields(EquipmentEvent(EventKind.STATE_CHANGED, "A", 0.0))
    with pytest.raises(ValueError):
        event_from_fields(EventKind.STATE_CHANGED, {**base, "NewState": "WEIRD"})


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


def test_extra_roundtrip_nested():
    extra = {"route": [[1, 2], [3, 4]], "battery_pct": 97.5}
    assert extra_from_json(snapshot_extra_to_json(extra)) == extra
