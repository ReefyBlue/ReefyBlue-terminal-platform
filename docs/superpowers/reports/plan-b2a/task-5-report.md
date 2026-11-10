# Task 5 report: Runner `--port http` and in-process equivalence

**Status:** DONE_WITH_CONCERNS (minor; see Concerns). Commit `9e7a151`.

## What
- `packages/runner/src/terminal_runner/run.py`
  - `run_once_http(scenario, layout, seed, *, ecs, gateway, client=None)` steps: `wait_healthy(HEALTH_TIMEOUT_S)`, or exit 1 → fleet check on `client.equipment()`, or exit 1 → `_connect(gateway)` → `prefill_yard` (same error as opcua) → `t0 = status()["sim_time"]` → `post_workorders` → `start_sim` → poll `/status` every `STATUS_POLL_S` → `kpis()` → `sim_elapsed_s = sim_time - t0`, `timed_out` → `stop_sim`. On failure after connect it does a best-effort `_stop_quietly` in `finally`, then `port.close()`. It closes the `EcsClient` only if it created it.
  - Constants: `STATUS_POLL_S = 0.5`, `HEALTH_TIMEOUT_S = 30.0`, `STALL_TIMEOUT_S = 120.0`. `_clock = time.monotonic` is a patch point for tests, like `_sleep`.
  - Stall: if `sim_time` does not change for 120 s wall time → `RuntimeError("sim time stalled")`.
  - The poll loop drains the runner's own port queue (`port.drain_events()`). Nothing subscribes to that port, so without the drain its queue would keep growing.
  - Refactor: `_fleet_mismatch(got, scenario, layout) -> str | None` is a pure function. `_check_fleet` and the http path both use it, and the message text is unchanged. `_fail(msg) -> NoReturn` does the log + stderr + exit 1 that `_connect`, `_check_fleet` and the http path all need. `_prefill_slots` and `_prefill_rejected` are shared with the opcua path.
  - CLI: `--port {inprocess,opcua,http}`, `--ecs` (default `http://localhost:8000`). `--replications > 1` with opcua or http gives the same parser error, naming the mode.
- `packages/runner/pyproject.toml`: unchanged (`terminal-tos-stub` and `terminal-kpi` were already deps).

## Tests
- `tests/runner/test_run_http.py` (14 tests): a scripted ECS via `httpx.MockTransport` → `EcsClient(transport=...)` passed as `client=`, a patched `_connect` fake port, and patched `_sleep`/`_clock`. Covered:
  - happy path: exact call order, KPIs + `sim_elapsed_s` + `timed_out == 0.0`, Stop/close
  - prefill comes before POST and Start when there are LOAD containers
  - prefill rejected → Stop + close, no POST
  - duration cap → `timed_out == 1.0`, elapsed measured from t0
  - stall → RuntimeError after exactly 240 polls, Stop, no `/kpis`
  - 422 → `WorkOrderRejected`, Stop, no Start
  - unhealthy → exit 1, `wait_healthy(30.0)`, no gateway connect
  - fleet mismatch → exit 1 with the shared message
  - `_fleet_mismatch` message text
  - own client closed, passed client not closed
  - CLI: replications error, ecs/gateway wiring, `--ecs` default
- `tests/e2e/test_service_equivalence.py` (marker `e2e`): exactly as the controller specified (TestClient, `portal.call(core.connect_once)`/`core.step`, `prefill` reused, body serialised like `EcsClient`, 60 s loop). It asserts `open == 0`, key order = fixture keys minus `timed_out`, and exact equality on every fixture key except `agv_wait_tp_s_mean`.

## RED / GREEN
- RED: with the `run.py` change stashed, `test_run_http.py` gave 14 failed (AttributeError: no `run_once_http` / `_fleet_mismatch` / `ecs` arg). Process note: I wrote the implementation before the http tests, then stashed it to get honest RED evidence before going GREEN.
- The equivalence test passed on first run. It pins down behaviour that Tasks 3–4 already implement, so it had no RED phase.
- GREEN: `uv run pytest tests/runner tests/e2e/test_service_equivalence.py -m "e2e or not e2e" -q` → 31 passed. With `test_plan_a_equivalence.py` added → 32 passed. `uv run pytest tests/test_architecture.py -q` → 29 passed. `ruff check` is clean.

## Equivalence result
Exact equality with `plan_a_baseline_seed1.json` on all compared keys (including `sim_elapsed_s` 11820.0). There are no deltas.

## Self-review
- The opcua path's behaviour and messages are unchanged (the existing `test_run_opcua.py` passes).
- `Kind(e["kind"])` raises ValueError on an unknown kind string from the service. I accepted this as a contract violation.
- Orders are generated before the health check. This is cheap and needs no network.

## Concerns
1. `sim_elapsed_s` is overwritten with `last_status.sim_time - t0`, as the controller decided. The rate KPIs from `/kpis` are computed over the service's own `now - core.t0`, read slightly later while the sim keeps running, and from the service's connect time. So in http mode `sim_elapsed_s` can differ slightly from the duration the rates were computed over (by the sim time that passes between the last `/status` and `/kpis`, plus any difference between t0s if the sim was not at 0 when the service connected). Alternative: keep the service's `sim_elapsed_s`.
2. `faults`: `EcsCore` builds its `KpiCollector` without `faults_total`, so it counts FAULT state events, as in opcua mode. This is equal to the baseline (0 faults), but it may differ from in-process for fault scenarios. This is outside Task 5 scope.
