# Plan B2a final-review fixes

Branch `feat/plan-b2a-ecs-service`. Commits on top of f42d1ca:
- `ba58124` fix(ecs-service): reconcile state on reconnect, detect sim restarts; runner freshness and link-loss fixes
- `6faeb43` docs(b2a): link-loss reconciliation limits, one ECS per sim run, job message, submit scope, spec drift

`packages/ecs` was not touched (the hash test passes as part of the full suite).

## Per finding

**1: reconnect reconciles state.** `packages/ecs_service/src/terminal_ecs_service/port.py`
- `bind` (l.40): on a re-bind (a snapshot cache already exists) it saves the old cache, refreshes it from the new port, then calls `_reconcile` (l.64). For each state difference `_reconcile` builds `EquipmentEvent(STATE_CHANGED, eid, sim_time=now, reason="LINK_RESTORED", new_state=fresh)` and delivers it through `_forward`, after the refresh. It logs one WARNING listing equipment whose `current_job_id` changed, e.g. `AGV-01 (J-1 -> None)`, and says that lost completions/failures are not reconciled.
- Added beyond the brief, and needed: `_track` (l.96). It updates the cached snapshot from forwarded events: STATE_CHANGED sets the state, and JOB_COMPLETED/JOB_FAILED clear the matching `current_job_id`. A successful `assign_job` (l.134) records the job id. Without this the cache only changed on `snapshot()` calls (network reads). A FAULT that arrived only as an event would leave a cached IDLE, so FAULT→IDLE during the outage would still go unnoticed. The cache served while unbound is now more current too.
- Tests:
  - `tests/ecs_service/test_reconnecting_port.py`: `test_rebind_synthesises_missed_state_changes` (FAULT→IDLE → one LINK_RESTORED IDLE event at the new sim time, and a callback that reads `snapshot()` sees IDLE), `test_rebind_with_unchanged_state_delivers_nothing`, `test_rebind_logs_current_job_changes` (caplog). `test_reconnecting_port_swaps_and_redelivers` was adjusted, because BUSY→IDLE across the swap is now reconciled.
  - `tests/ecs_service/test_ecs_core.py::test_reconnect_readmits_device_that_recovered_during_the_outage` is a behaviour test: after reconnecting, `submit` assigns to AGV-01 on the new FakePort. With `_reconcile` stubbed out I checked that it fails: no assignment, no LINK_RESTORED message.
- Docs: README "Verbindingsverlies" (README.md:167) and spec §7 table.

**2: sim restart under a running ECS.**
- (a) `core.py`: `_last_now` and `time_reversed` (l.81-82). `_check_time` (l.119) runs in `connect_once` (l.105) and in `step` (l.169). It logs one ERROR `sim time went backwards (%.1f -> %.1f): restart the ECS service per sim run`, sets the flag, and the loop keeps running. `app.py` `/health` (l.161-169) answers 503 `{"connected": true, "sim_time": t, "error": "sim_time_reversed"}`. `schemas.HealthOut.error: str | None = None` (schemas.py:34). The route uses `response_model_exclude_none=True`, so the 200 body is unchanged (`{"connected","sim_time"}`).
- (b) `run.py` `run_once_http` (l.298): after `wait_healthy` it reads `client.status()` and exits 1 when `done + open + failed > 0`, with "ECS service … is not fresh (N orders from an earlier run: done x, open y, failed z): restart it per run". This happens before connecting to the gateway, so the sim gets nothing.
- (c) README.md:143: one ECS process per sim run until B2c. Spec §3 `/health` row and §7 table updated.
- Tests:
  - `test_ecs_core.py::test_sim_time_going_backwards_flags_and_health_503`: reconnect to a restarted FakePort, then the flag, the exact ERROR text, `/health` 503 body via the ASGI app, and the loop still steps.
  - `test_ecs_core.py::test_sim_time_going_backwards_detected_in_step`: same link, logged once.
  - `tests/runner/test_run_http.py::test_not_fresh_ecs_exits_1`, parametrised over done/open/failed.
  - `ScriptedEcs` now serves a fresh status for the first `GET /status`, and the expected call logs include it.

**3: `sim_elapsed_s` in http mode.** `run.py` l.320-324: the KPIs keep `/kpis`' `sim_elapsed_s`; only `timed_out` is set. `_poll_until_done` now returns just `timed_out`. Tests: `test_run_once_http_happy_path` expects `{**KPIS, "timed_out": 0.0}` with the key order of `/kpis`; `test_timed_out_at_duration_cap` expects the service's value.

**4: README WS `job` message.** README.md:160 now reads: newly assigned job, after each tick and after `POST /workorders`, `sim_time` = `assigned_at`.

**5: `drain_events` after link loss.**
- `_advance`: the final `drain_events()` moved inside the `try` (run.py l.185), so a ConnectionError there becomes `RuntimeError("sim time stalled: …")`. Test: `tests/runner/test_run_opcua.py::test_advance_wraps_connection_error_of_the_final_drain`.
- `_poll_until_done` (l.340-368): a ConnectionError from the runner's drain logs one warning and draining stops; `Stop` goes through `_stop_quietly` / `stop_sim` as before. Test: `test_run_http.py::test_runner_link_loss_while_polling_does_not_fail_the_run` (one drain, one warning, run completes, then kpis → stop → close).

**8: `WorkOrderErrorsOut`.** `app.py` l.125: `POST /workorders` declares `responses={422: WorkOrderErrorsOut, 500: WorkOrderErrorsOut, 503: description}`, and `/health` declares 503 → `HealthOut`. In OpenAPI, 422 now references `WorkOrderErrorsOut` instead of FastAPI's `HTTPValidationError`. Test: `tests/ecs_service/test_app.py::test_openapi_declares_workorder_errors_and_health_503`.

**9: spec drift.** Spec §6 now has `port.py` (`ReconnectingPort`), plus `pyyaml`/`anyio` for `terminal_ecs_service` and `httpx2` in the dev group (Starlette TestClient). The "new dependencies only" line in the plan's Global Constraints has the same addition.

**10: README `submit` scope.** README.md:141 says `submit` neither prefills LOAD containers nor starts the sim (LOAD orders would fail with SLOT_EMPTY). Use the runner `--port http` for a full run, or DISCHARGE-only scenarios with `submit`. The process table row points to that note.

## Verification
- `uv run pytest tests/ecs_service tests/runner tests/opcua/test_opcua_port.py tests/e2e/test_service_equivalence.py tests/test_architecture.py -q` → **136 passed** in 26.8 s, pristine (only uv's `UV_NATIVE_TLS` notice).
- `uv run pytest tests/e2e/test_service_small.py -m e2e_opcua -q` → **1 passed** in 43.9 s.
- `uv run pytest -q` → **451 passed, 4 deselected** in 258 s.
- `ruff check` on all changed .py files: clean. The repo-wide ruff errors were already there in untouched files.

## Concerns
- Reconciliation runs inside `bind`, so the synthesised `equipment` WS messages (LINK_RESTORED) come before the `link` connected=true message. If a callback raises ConnectionError during that delivery, `connect_once` already handles it as "lost while binding".
- A device that goes IDLE→FAULT during the outage also gets a synthesised FAULT. The KpiCollector counts it as a fault and the Dispatcher replans its job: intended, but it is a behaviour change.
- Lost JOB_COMPLETED/JOB_FAILED are still unreconciled (WARNING only). The Dispatcher watchdog eventually cancels stuck AGV jobs; STS/ASC jobs stay open until B2d.
- The cached `current_job_id` is best-effort (events plus assign_job). The job-change warning can miss a change if a job started and ended on the gateway side without the ECS seeing it.
- `time_reversed` is sticky; only restarting the service clears it, by design.
