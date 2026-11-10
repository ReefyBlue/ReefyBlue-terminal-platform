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


def event_to_json(ev: EquipmentEvent) -> str:
    return json.dumps(asdict(ev))


def event_from_json(s: str) -> EquipmentEvent:
    d = _loads(s)
    try:
        ns = d["new_state"]
        return EquipmentEvent(
            kind=EventKind(d["kind"]),
            equipment_id=d["equipment_id"],
            sim_time=d["sim_time"],
            job_id=d["job_id"],
            reason=d["reason"],
            new_state=None if ns is None else State(ns),
        )
    except KeyError as e:
        raise ValueError(f"missing field: {e.args[0]}") from e


def snapshot_extra_to_json(extra: Mapping[str, Any]) -> str:
    return json.dumps(dict(extra))


def extra_from_json(s: str) -> dict[str, Any]:
    return _loads(s)
