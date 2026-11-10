# Task 4 report — FastAPI app, schemas, serve CLI

Status: DONE_WITH_CONCERNS (minor). Commit: `cff7534`.

## What
- `schemas.py`: `WorkOrderIn` (type `Literal["DISCHARGE","LOAD"]`), `WorkOrderErrorOut` (`container_id: str | None`), `WorkOrderErrorsOut`, `AcceptedOut`, `HealthOut`.
- `app.py`: `create_app(core, *, run_loop=True)`. The lifespan starts `core.run()` as a task and cancels and awaits it on shutdown. `validate_orders(core, orders)` collects at most one error per order, checked in the same order as `Dispatcher.submit`:
  1. `DUPLICATE_CONTAINER`: a repeat within the batch, or a container already in `core.known_containers`.
  2. `UNKNOWN_STS`: not an STS in `core.equipment()`.
  3. `INVALID_SLOT`: `parse_location` fails, or the result is not a `SlotLoc`, or its block is not in `allocator.blocks()` with an ASC serving it.

  Any error gives 422 `{"errors":[...]}` and nothing is submitted. If not connected: 503. A `WorkOrderError` that gets past validation gives 500 plus a log line (spec §7). `/status`, `/kpis`, `/equipment` and `/workorders` catch `ConnectionError`, call `core.on_link_lost()` and answer 503. `/kpis` answers 503 while there is no KPI collector. `/health` answers 200 `{"connected":true,"sim_time":t}` or 503 `{"connected":false,"sim_time":null}`. `WS /events` registers its queue, sends `core.status_message()`, then pumps the queue. An anyio task group also watches for `websocket.disconnect`, so a client that leaves while nothing is being sent is still unregistered.
- `serve.py`: `main(argv)` with `--gateway --layout --host 0.0.0.0 --port 8000 --log-level info`, `configure_json_logging`, `uvicorn.run(app, host, port, log_config=None, log_level)`. `load_layout` uses pyyaml to read only the blocks (no `terminal_sim` import). `port_factory(gateway)` builds `OpcUaEquipmentPort(gateway)`, calls `.connect()` and returns it.
- Ruling A: `OpcUaEquipmentPort.drain_events()` calls `_check_lost()` after the queue is empty, so buffered events are delivered first and then `ConnectionError` is raised. The docstring is updated.
- Ruling B (`core.py`):
  - `on_link_lost()` unbinds and publishes `link` at once, then closes the old link through `loop.run_in_executor`; close errors are logged. With no running loop it closes inline. Cancelling `run()` still closes synchronously in its `finally`.
  - `submit()` raises `RuntimeError("not connected")` when `not self.connected`.
  - `run()` logs any non-`ConnectionError` exception with `log.exception`, calls `on_link_lost()` if still connected, sleeps `CONNECT_RETRY_S` and keeps looping.
  - New `known_containers` set and `status_message()`; `publish_status` now uses `status_message()`.
- `SimpleSlotAllocator.blocks()` added (layout order).
- Dependencies: ecs_service gets `pyyaml>=6` and `anyio>=4`. Root dev group gets `httpx2`: without it, starlette 1.x's `TestClient` raises a `StarletteDeprecationWarning`, which would break the requirement for clean test output. `uv.lock` is updated.

## Tests
- `tests/ecs_service/test_app.py` (20): every test in the brief, plus a duplicate against an earlier batch, a TP location rejected as INVALID_SLOT, an AGV used as `sts_id`, a bad `type` returning INVALID_ORDER in the errors format, `ConnectionError` returning 503 on each of the 4 endpoints (parametrized), WS status before any connection, the lifespan starting and cancelling `run()`, and `serve.load_layout`/`port_factory`.
- `tests/ecs_service/test_ecs_core.py`:
  - New: submit while the link is lost, `known_containers`, off-loop close that does not block (slow close of 0.3 s, `on_link_lost` returns in under 0.1 s), close failure logged, `run()` surviving an unexpected exception and reconnecting, `known_containers` kept when `ConnectionError` hits after the Dispatcher took the orders.
  - Changed: `test_step_on_connection_error_unbinds_and_publishes_link` now waits for the off-loop close. This is required by ruling B.
- `tests/opcua/test_opcua_port.py::test_drain_events_raises_after_connection_lost` (offline unit test, no rig).
- `tests/tos_stub/test_allocator.py::test_blocks_in_layout_order`.

RED: `test_app.py` failed at import (`cannot import name 'serve'`). In core and opcua, 5 failed (submit-not-connected, known_containers, off-loop close, run-survives, drain-raises). Exception: the last test (`known_containers` after a late `ConnectionError`) was written after its fix. It fails on the old code by construction, but I did not run it RED.

GREEN: `uv run pytest tests/ecs_service tests/opcua/test_opcua_port.py tests/test_architecture.py -q` gives 91 passed with clean output. Full default suite: 421 passed, 3 deselected (4m22s; this was before the final `known_containers` edit). After that edit, ecs_service + opcua_port + architecture + tos_stub gave 118 passed. `test_app.py` was run 10 times in a row with no flakes.

## Self-review / notes
- The first WS version used raw `asyncio.create_task` and `asyncio.wait`. It leaked a `CancelledError` through the TestClient's anyio cancel scope (a race between disconnect and cancel). Moving to an anyio task group fixed it.
- WS tests drive the core through `client.portal.call(...)` (`connect_once`, `step`, `on_link_lost`), so `Broadcaster.publish` runs on the app loop's thread.
- `/status` and `/equipment` stay 200 while disconnected, answering from the last-known state (ReconnectingPort). Only `/kpis` without a collector, `/workorders` while not connected, and `/health` return 503.

## Concerns
1. **`INVALID_ORDER` is a new error code** for body-shape errors (for example a bad `type` or a missing field). The spec lists only 3 codes, but without this the client's `WorkOrderRejected` would get `errors=[]` from FastAPI's `{"detail": ...}`. `container_id` is null when it cannot be recovered from the body. The controller may prefer the plain FastAPI default.
2. Endpoints and validation call `core.equipment()` / `snapshot`. Over OPC UA these are blocking round trips on the loop thread, the same as the Dispatcher already does. This is inherent to the design, but `POST /workorders` reads the whole fleet once per batch.
3. `core.submit` occupies slots before `Dispatcher.submit`. If the Dispatcher still raises `WorkOrderError` (only possible if validation misses something), those slots stay occupied. Validation mirrors the Dispatcher's checks, so this should not happen in practice.
4. `httpx2` was added as a dev dependency to keep test output clean.

## Files changed
- packages/ecs_service/src/terminal_ecs_service/{app,schemas,serve}.py (new), core.py
- packages/ecs_service/pyproject.toml, pyproject.toml, uv.lock
- packages/opcua/src/terminal_opcua/port.py
- packages/tos_stub/src/terminal_tos_stub/allocator.py
- tests/ecs_service/test_app.py (new), tests/ecs_service/test_ecs_core.py, tests/opcua/test_opcua_port.py, tests/tos_stub/test_allocator.py

## Fix round 1

**IMPORTANT 1: 503 although the orders were accepted.** `core.submit` now separates failures that happen before the Dispatcher takes the orders from those that happen after:
- Before take, it raises as before: `RuntimeError("not connected")`, or `WorkOrderError` from `Dispatcher.submit`'s validation, which does not touch the port.
- After take, a `ConnectionError` from `Dispatcher.submit`'s internal tick or from core's own follow-up `tick()` is caught. `core.submit` records `known_containers`, logs a warning, calls `on_link_lost()`, publishes the new jobs and returns normally. The handler therefore answers 202 `{"accepted": n}`.
- A `ConnectionError` during validation (`core.equipment()`) still comes before the take and still gives `guarded` → 503.
- Other exceptions after the take still record the containers and re-raise.

**MINOR 2.** The bind-failure path in `connect_once` now uses `_drop_link(blocking=False)`, so the close runs off the loop. The shutdown close in `run()`'s `finally` is still blocking.

**Covering tests:**
- New `test_app.py::test_workorders_202_when_link_drops_after_take`: `assign_job` raises in the tick. The test checks 202 `{"accepted":2}`, `known_containers == {"C1","C2"}`, Dispatcher `open == 2`, `/health` 503, and that a resubmit gives 503.
- New `test_ecs_core.py::test_connect_once_bind_failure_closes_off_loop`: a 0.3 s close does not block, and the port does close later.
- Updated `test_ecs_core.py::test_submit_connection_error_after_take_still_records_containers`: no raise now, the link is dropped and `open == 1`.

**RED:** with the old core.py stashed, all 3 tests failed (`3 failed, 46 passed`).

**GREEN:** `uv run pytest tests/ecs_service -q` → `49 passed in 1.40s`. `uv run pytest tests/ecs_service tests/opcua/test_opcua_port.py tests/test_architecture.py -q` → `93 passed in 23.33s`. `ruff check packages/ecs_service tests/ecs_service` is clean.
