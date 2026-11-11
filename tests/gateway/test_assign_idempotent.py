# ruff: noqa: F811 - tests take the imported `rig` fixture as a parameter
"""``AssignJob`` on the gateway is idempotent for a job the upstream already runs or ran.

The blip flake: the gateway's forwarded ``AssignJob`` times out during a cut while the
upstream executed it; the ECS rejects the job (ignoring its outcome event) and later
re-issues the SAME job id. Forwarding it ran the job again. Now a job id equal to the
mirrored ``CurrentJobId`` is answered True without forwarding, and one equal to
``LastJobId`` is answered True with the recorded ``LastJobOutcome`` re-emitted.

Runs on the ``rig`` harness of ``test_gateway.py``: a real upstream ``EquipmentServer``
(STS-01) and a real gateway; the upstream's variables reach the mirror through its
subscription and the gateway's ``AssignJob`` method is called over OPC UA.
"""

import logging
from types import SimpleNamespace

import pytest
from terminal_model.port import EventKind
from terminal_model.types import Job, JobType
from terminal_opcua.codec import job_to_json
from test_gateway import (  # noqa: F401 - harness fixture
    _start_connected,
    _sts,
    _val,
    _wait_for,
    _wait_until,
    rig,
)

JOB_ID = "C1-DISCHARGE-1"


def _job_json(job_id: str = JOB_ID) -> str:
    return job_to_json(Job(job_id, JobType.DISCHARGE, "C1", "VESSEL", "STS-01"))


async def _start_counting(r) -> list[str]:
    """Start the rig with the upstream's ``AssignJob`` handler counting its calls."""
    calls: list[str] = []
    real = r.server._assign

    def counting(job_json: str) -> bool:
        calls.append(job_json)
        return real(job_json)

    r.server._assign = counting  # bound into the upstream's method handler at start()
    await _start_connected(r)
    return calls


async def _upstream(r, *, current: str | None = None, last: str | None = None,
                    outcome: str | None = None) -> None:
    """Set the upstream's CurrentJobId / LastJobId / LastJobOutcome and wait until the
    gateway mirror shows them."""
    r.sts.current_job = SimpleNamespace(job_id=current) if current else None
    r.sts.last_job_id, r.sts.last_job_outcome = last, outcome
    await r.server.sync()
    want = {"CurrentJobId": current or "", "LastJobId": last or "", "LastJobOutcome": outcome or ""}
    for name, value in want.items():
        assert await _wait_for(lambda name=name, value=value: _is_val(r, name, value), 2.0), name


async def _is_val(r, name, value) -> bool:
    return await _val(r, name) == value


async def _assign(r, job_json: str) -> bool:
    return await r.obj.call_method(f"{r.idx}:AssignJob", job_json)


def _outcomes(r) -> list[tuple[EventKind, str | None, str | None]]:
    return [(ev.kind, ev.job_id, ev.reason) for _, ev in _sts(r)
            if ev.kind in (EventKind.JOB_COMPLETED, EventKind.JOB_FAILED)]


async def test_assign_idempotent_on_current_job(rig, caplog):
    calls = await _start_counting(rig)
    await _upstream(rig, current=JOB_ID)
    with caplog.at_level(logging.INFO, logger="terminal_gateway.mirror"):
        assert await _assign(rig, _job_json()) is True
    assert calls == []
    assert any(r.levelno == logging.INFO and r.getMessage() ==
               f"assign {JOB_ID}: already CurrentJobId upstream, idempotent True" for r in caplog.records)
    assert _outcomes(rig) == []  # the job still runs: its own outcome event follows


@pytest.mark.parametrize(("outcome", "expected"), [
    ("COMPLETED", (EventKind.JOB_COMPLETED, JOB_ID, None)),
    ("FAILED", (EventKind.JOB_FAILED, JOB_ID, "FAULT_DURING_OUTAGE")),
])
async def test_assign_idempotent_on_last_job(rig, outcome, expected):
    calls = await _start_counting(rig)
    await _upstream(rig, last=JOB_ID, outcome=outcome)
    assert await _assign(rig, _job_json()) is True
    assert calls == []
    assert await _wait_until(lambda: _outcomes(rig) == [expected], 1.0), _outcomes(rig)


async def test_assign_forwards_new_job(rig):
    calls = await _start_counting(rig)
    await _upstream(rig, current="C0-DISCHARGE-1", last="C9-DISCHARGE-1", outcome="COMPLETED")
    await _assign(rig, _job_json())
    assert calls == [_job_json()]
    assert _outcomes(rig) == []


async def test_assign_forwards_when_nothing_mirrored_or_malformed(rig):
    """Empty mirrored ids (no job yet) never match; malformed JSON is forwarded and the
    upstream rejects it."""
    calls = await _start_counting(rig)
    assert await _assign(rig, "not json") is False
    assert calls == ["not json"]
    await _assign(rig, _job_json())
    assert calls == ["not json", _job_json()]
