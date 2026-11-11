# Task 6 report: API endpoints, CLI flags, runner

**Status:** DONE. Commit `8e5b043` on `feat/plan-b2d-link-loss`.

## Changes
- `app.py`: `GET /alarms` (sorted by since_sim_time, kind, equipment_id, job_id; works while the link is down: it shows the last-known alarms, `[]` before the first connection); `POST /equipment/{equipment_id}/release` returns 200 `{"released"}`, 404 `UNKNOWN_EQUIPMENT`, 409 `RELEASE_REFUSED` (message = `ReleaseRefused.reason`) or 503; `POST /jobs/{job_id}/resolve` (`ResolveIn`) returns 200, 404 `UNKNOWN_JOB`, 422 (Pydantic) or 503. All go through `guarded()`, so a ConnectionError drops the link and returns 503. Release and resolve check `core.connected` first and return 503, the same way `/workorders` does, so `EcsCore`'s `RuntimeError` is never reached.
- `schemas.py`: added `AlarmOut`, `ReleaseOut`, `ResolveIn`, `ResolveOut`, `RefusedOut`, all declared in `responses=`. A test checks them in the OpenAPI output.
- `serve.py`: added `--auto-release` (store_true), which goes to `EcsCore(auto_release=...)`.
- `run.py`: `run_once` and `run_once_opcua` take a keyword `auto_release=True`. `main` adds `--no-auto-release` and passes it through `functools.partial` (in-process and opcua). In http mode the flag has no effect, and `--help` says so. `_wait_ready` now waits out FAULT **or OFFLINE** and uses `_clock` (patchable).
- `client.py`: added `EcsRefused(status, code, message)` (raised on 404/409) and `alarms()`, `release()`, `resolve_job()`. 422 and 503 raise `httpx.HTTPStatusError`.
- `tests/e2e/opcua_procs.py`: `_gateway_ready` is not ready while any state is FAULT or OFFLINE. The `ecs_service` helper now also waits until `GET /equipment` is 200, non-empty and has no FAULT/OFFLINE.

## Tests
- New tests in `test_app.py`: the 8 named in the brief, plus `test_release_connection_error_drops_link_and_503` and `test_openapi_declares_operator_endpoints`. `test_resolve_200_completed_and_failed` is parametrized and also covers the 409 `"resolve job <id> first"` case.
- New tests in `test_run.py`: `auto_release` default and passthrough; the `--no-auto-release` CLI reaching the Dispatcher (in-process, patched `Dispatcher`) and reaching the opcua partial; http mode ignores the flag; `--help` text; `_wait_ready` with OFFLINE→FAULT→IDLE and with a timeout on OFFLINE.
- New tests in `test_client.py`: the three methods, 409/404 → `EcsRefused`, and 422/503 → `HTTPStatusError`.
- `uv run pytest tests/ecs_service tests/runner tests/tos_stub tests/e2e/test_plan_a_equivalence.py tests/e2e/test_faults_equivalence.py -q`: **151 passed**.
- Also ran `-m e2e_opcua` on `test_service_small.py` and `test_opcua_small.py`: 2 passed.
- ruff: no new findings (57 before and after; all of them predate this task).

## Concerns / notes
- `run_once_http` has no FAULT/OFFLINE readiness wait of its own. It only has the `/health` wait and the fleet check, and I added nothing because the ruling said "if any". After `/health` 200 the service may still briefly report OFFLINE devices. The e2e `ecs_service` helper now covers this for tests.
- The e2e `ecs_service` helper has no way to pass `--auto-release` yet. Task 7 (proxy blip with service `--auto-release`) will need to add an extra-args parameter.
