"""JSON codecs for OPC UA String variables (jobs, events, snapshot extras)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from terminal_model.port import EquipmentEvent, EventKind
from terminal_model.types import Job, JobStatus, JobType, State


def _loads(s: str) -> dict[str, Any]:
    try:
        d = json.loads(s)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from e
    if not isinstance(d, dict):
        raise ValueError("expected a JSON object")
    return d


def job_to_json(job: Job) -> str:
    return json.dumps(asdict(job))


def job_from_json(s: str) -> Job:
    d = _loads(s)
    try:
        return Job(
            job_id=d["job_id"],
            type=JobType(d["type"]),
            container_id=d["container_id"],
            from_loc=d["from_loc"],
            to_loc=d["to_loc"],
            priority=d["priority"],
            deadline=d["deadline"],
            status=JobStatus(d["status"]),
        )
    except KeyError as e:
        raise ValueError(f"missing field: {e.args[0]}") from e


def event_fields(ev: EquipmentEvent) -> dict[str, object]:
    """OPC UA event field values for an equipment event (Seq/kind are set by the emitter)."""
    return {
        "EquipmentId": ev.equipment_id,
        "SimTime": ev.sim_time,
        "JobId": ev.job_id or "",
        "Reason": ev.reason or "",
        "NewState": ev.new_state.value if ev.new_state is not None else "",
    }


def event_from_fields(kind: EventKind, fields: Mapping[str, object]) -> EquipmentEvent:
    try:
        new_state = str(fields["NewState"])
        return EquipmentEvent(
            kind=kind,
            equipment_id=str(fields["EquipmentId"]),
            sim_time=float(fields["SimTime"]),  # type: ignore[arg-type]
            job_id=str(fields["JobId"]) or None,
            reason=str(fields["Reason"]) or None,
            new_state=State(new_state) if new_state else None,
        )
    except KeyError as e:
        raise ValueError(f"missing field: {e.args[0]}") from e


def snapshot_extra_to_json(extra: Mapping[str, Any]) -> str:
    return json.dumps(dict(extra))


def extra_from_json(s: str) -> dict[str, Any]:
    return _loads(s)
