# Task 3 report — Dispatcher: OFFLINE, vrijgave, onbekende uitkomst, alarmen

Commit: `74ee360 feat(ecs): OFFLINE handling, operator release, unknown-outcome resolution and alarms`

## What
- New `terminal_ecs/alarms.py`: `AlarmKind` (StrEnum: LINK_LOST, NEEDS_RELEASE, UNKNOWN_OUTCOME), frozen `Alarm(kind, equipment_id, job_id, since, detail)`, `ReleaseRefused(reason)` with `.reason`. Re-exported from `terminal_ecs/__init__.py`. Imports stdlib only.
- `Dispatcher(..., auto_release=False)` plus the new state `_offline`, `_offline_job`, `_needs_release`, `_unknown`, `_alarm_since` and the precomputed `_all_eq`.
- `_on_state` checks in this order: unknown equipment is ignored with a warning (any state); OFFLINE goes to `_go_offline`; an event for an offline device goes to `_come_back` (it warns if the event has no LINK_RESTORED/UPSTREAM_RECONNECTED reason); anything else goes to `_apply_state` (IDLE/FAULT per §5.2).
- `_come_back`: BUSY means the job runs on. IDLE while the remembered job is still in `_job_eq` means its outcome is unknown: the job goes in `_unknown` and the device in `_needs_release`. With `auto_release`, the job is resolved as `failed` at once and the IDLE re-admits the device. FAULT is handled as in §5.2. An OFFLINE return never gets here: it goes through `_go_offline`, which does nothing for a device already offline.
- On JOB_COMPLETED/JOB_FAILED (`on_event`), `_outcome_seen` clears `_offline_job` and any `_unknown` entry for that job.
- `release`, `resolve_job`, `alarms` and the three new `status()` counters are as specified. The alarm detail strings are exact. `alarms()` sorts by (since, kind, equipment_id, job_id).
- Watchdog: an AGV job is frozen (`_last_progress = now`, skipped) while the AGV is offline **or while the job's outcome is unknown**. The second case is an addition: without it the watchdog would cancel the unresolved job after 600 s.
- Available-check: `_available` returns False for devices that are offline or in `_needs_release`.
- `_unusable()` (excluded ∪ quarantined ∪ offline ∪ needs_release) replaces `excluded | quarantined` in the crane-head check and the AWAIT_TP check. Effect: the STS does not serve an AGV that is offline or unreleased.
- `_asc_down` also counts an ASC in `_needs_release` as down.
- With `auto_release` this changes nothing, because `needs_release` stays a subset of `excluded`.
- Runner: `run_once` and `run_once_opcua` now use `Dispatcher(..., auto_release=True)`.
- Tests:
  - `tests/ecs/fake_port.py` gets `make_dispatcher()`, the one place that sets `auto_release=True` (all inline constructions in the discharge/load/recovery tests switched to it).
  - `FakePort.set_state(..., reason=None)` and `FakePort.emit()` added.
  - `tests/opcua/test_port_reconnect_ecs.py` updated. On the blip it asserts OFFLINE with offline=1, replans=0, needs_release=0. After reconnect it asserts IDLE, all counters 0, no alarms, and that the AGV is assigned again. No job runs during the blip, so the test stays deterministic and keeps default `auto_release=False`.

## Tests
- New `tests/ecs/test_dispatcher_offline.py` (13 tests) and `tests/ecs/test_dispatcher_release.py` (7 tests). They cover every case in the brief, plus: a second OFFLINE on return stays offline, a completion during OFFLINE clears `_offline_job`, resolve-failed replans and the crane waits for release, and the status key order.
- RED: collection errors (no `terminal_ecs.alarms`, no `auto_release` keyword).
- GREEN:
  - `tests/ecs` + plan-A equivalence + faults equivalence + `tests/kpi`: 87 passed.
  - `test_port_reconnect_ecs.py` + architecture without the hash test: 29 passed.
  - Ruff is clean on the changed source files and new tests. The 2 ruff findings in `test_dispatcher_discharge.py:181` and `test_port_reconnect_ecs.py:29` were there before this task.
- Equivalence: `test_plan_a_equivalence` (baseline seed 1, exact) and `test_faults_equivalence` (seed 1) are both green.
- The hash test `tests/test_architecture.py::test_ecs_unchanged_since_plan_a` fails on purpose until Task 7.
- Full suite (hash test deselected): 472 passed, 1 failed (see Concerns).

## Self-review
- `auto_release=True` matches today's behaviour. FAULT adds the device to both `excluded` and `needs_release`, and IDLE clears both. An unknown outcome is replanned and then re-admitted by the IDLE, which is the same net effect as B2a's FAULT→replan→IDLE.
- An event for unknown equipment is now ignored for every state, not only OFFLINE. Before, a FAULT for an unknown id only touched `_excluded`, which did nothing. Now it would also raise a bogus NEEDS_RELEASE alarm, so ignoring it is safer.
- `orders.py` not touched.

## Concerns
- `tests/ecs_service/test_ecs_core.py::test_reconnect_readmits_device_that_recovered_during_the_outage` now fails.
  - Cause: EcsCore builds its Dispatcher with the default `auto_release=False`, so after a FAULT the AGV needs a release.
  - This is the intended B2d behaviour. Tasks 4/5 change this path: ReconnectingPort will emit LINK_LOST/LINK_RESTORED, and EcsCore gets an `auto_release` parameter. That test should be rewritten there; I did not touch `core.py` because it is outside this task.
- The crane does not serve an offline or unreleased AGV. The spec does not say this explicitly; it is a safety choice that does not apply in auto-release in-process runs.
- An offline ASC does not count as "down" for re-slotting, so OFFLINE triggers no reslot-replans.
- If JOB_COMPLETED/JOB_FAILED arrives late for a job whose outcome is unknown, the job is processed normally and drops out of `_unknown`. The device stays in `needs_release` until the operator releases it.

## Fix round 1 (review of 74ee360)

**IMPORTANT 1: a FAULT replanned a job whose outcome was unknown**
- In `_apply_state(FAULT)`, the replan loop now skips jobs that are in `_unknown`. Those jobs keep their stage and wait for `resolve_job`, as §5.3 requires.
- `_resolve` now logs a warning when the job is no longer running (`rec is None`), instead of returning silently.

**Minor (a):** `status()["bound"]` now uses `_unusable()`. An offline AGV carrying a box, or an AGV that has not been released, now counts as bound.

**Minor (b):** NEEDS_RELEASE alarm detail now depends on why the device needs release.
- A new `_release_cause` dict records the first cause per device. It is set when the device is added to `_needs_release` and cleared by `_clear_release`.
- The detail reads `"unknown outcome: release required"` when the cause was a lost job outcome, and `"fault: release required"` when it was a FAULT.
- With `auto_release` this changes nothing: `needs_release` is still a subset of `excluded` there.

**Tests**
- `test_fault_after_unknown_outcome_keeps_job_for_operator`: unknown outcome on the STS, then FAULT, then IDLE, then `resolve_job(..., "completed")`. Checks that the order goes on to the TO_YARD leg, `replans == 0`, and the alarm detail is "unknown outcome: release required".
- `test_bound_counts_offline_agv_with_box`: the box is on an offline AGV, so bound is 1. After the AGV returns IDLE, bound is 0 and the TO_YARD leg is issued.
- `test_needs_release_detail_for_fault` (release tests): checks the "fault" detail.
- `test_offline_idle_return_with_job_is_unknown_outcome`: expected detail updated to the "unknown outcome" text.

**Command:** `uv run pytest tests/ecs tests/e2e/test_plan_a_equivalence.py tests/e2e/test_faults_equivalence.py tests/opcua/test_port_reconnect_ecs.py -q`

**Output:** `75 passed in 12.44s`. The plan A and faults equivalence tests are still exact. Ruff is clean.
