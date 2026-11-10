# Task 3 report — `terminal_ecs_service` core

Status: DONE_WITH_CONCERNS. Commit `dc29adc` feat(ecs_service): ReconnectingPort, Broadcaster and EcsCore loop.

## What
- `packages/ecs_service` (pyproject mirrors gateway; deps model, ecs, opcua, kpi, tos-stub, fastapi>=0.115, uvicorn[standard]>=0.30). Registered in root `pyproject.toml` (dev group, uv sources, pytest pythonpath). `uv sync` done; `uv.lock` updated.
- `port.py` `ReconnectingPort`: `bind/unbind/connected/drain_events` plus the full `EquipmentPort`. Callbacks are kept. Each `bind` subscribes one forwarder tagged with a generation number, which is bumped on every bind/unbind. A stale port therefore stops delivering, and binding the same port again does not deliver events twice. `bind` caches `now()`, the ids and every snapshot. `snapshot()`/`equipment_ids()`/`now()` refresh the cache while bound and serve it while unbound. While unbound, commands return `False` with a warning. `drain_events` delegates when the bound port has it, else returns 0.
- `events.py` `Broadcaster`: `QUEUE_SIZE=1000`, `WARN_INTERVAL_S=60.0`, `register/unregister/publish/clients`. When a client's queue is full, the message is dropped for that client only, with at most one warning per client per interval (it includes a dropped count).
- `core.py` `EcsCore` follows the brief: `DRAIN_POLL_S`, `CONNECT_RETRY_S`, `SAMPLE_INTERVAL_S` (re-exported from `terminal_kpi.kpi`), a `LayoutLike` Protocol, the allocator built from `layout.blocks`, and Dispatcher/KpiCollector created once at the first connect (`faults_total=None`). It provides `connect_once`, `step` (sync), `on_link_lost`, `run`, `submit`, `status`, `kpis`, `equipment`, `publish_status` and `publish_new_jobs` (`_seen_jobs`). Equipment events are broadcast through `port.subscribe`. Message shapes match spec §5: enum fields in `equipment` messages are sent as `.value`, and `status` carries `Dispatcher.status()` unchanged (its State values are StrEnums).
- `tests/test_architecture.py`: `"ecs_service"` added to `PACKAGES` and `ALLOWED`.

Choices that go beyond the brief:
- `connect_once` runs `connect()` through `asyncio.to_thread`, because OPC UA `connect()` blocks for up to about 2 s plus setup. The port is documented as safe from any non-loop thread. `bind` runs on the loop.
- A `ConnectionError` raised during `bind` drops the link and returns False.
- `t0` is set only on the first connect. On a reconnect where `now >= next_tick`, `next_tick` moves to the next 60 s boundary from `t0`, so there is no burst of catch-up ticks and KPI elapsed stays measured from the original `t0`.
- `on_link_lost` unbinds and also closes the old link if it has `close()` (spec §4 "port sluiten"). `run()` closes the link in `finally`.
- `submit` raises `RuntimeError` when there is no dispatcher; the API (Task 4) should answer 503 before calling it. `submit` also calls `dispatcher.tick()` as the brief says, which is redundant because `Dispatcher.submit` already ticks.
- Status when no dispatcher exists: all counters are 0 and `equipment` is `{}`.

## Tests
`tests/ecs_service/test_reconnecting_port.py` (6), `test_broadcaster.py` (3), `test_ecs_core.py` (12). They cover every scenario in the brief, plus: a late subscriber, rebinding the same port, drain delegation, the warning repeating after the interval, unregister, reconnect keeping the dispatcher and `t0` with the next boundary realigned, `run()` retry-then-step, the `equipment()` shape, and status/kpis while connected.
Test files were renamed from the brief's `test_port/test_events/test_core.py`: the basenames `test_port.py` and `test_events.py` collide with `tests/model/test_port.py` and `tests/opcua/test_events.py` (rootdir import mode, no `__init__`), and the full suite failed at collection.

RED: `ModuleNotFoundError: No module named 'terminal_ecs_service'` (3 collection errors).
GREEN: `uv run pytest tests/ecs_service tests/test_architecture.py -q -W error` → 50 passed. Full suite: 394 passed, 3 deselected. `ruff check` is clean (the repo is not ruff-formatted, so the files were left unformatted).

## Files
packages/ecs_service/{pyproject.toml, src/terminal_ecs_service/{__init__,port,events,core}.py}; tests/ecs_service/*; pyproject.toml; tests/test_architecture.py; uv.lock. Line counts: core.py 232, port.py 108, events.py 50.

## Concerns
1. **Production link-loss detection.** `OpcUaEquipmentPort.drain_events()` never raises `ConnectionError`: it does not call `_check_lost()`. `assign_job/cancel_job/reset` swallow errors and return False through `_call_bool`, and `now()` stays frozen after a loss. Only `snapshot()` (inside `dispatcher.tick`/`kpi.sample`/`status`) raises, and those calls only happen on a 60 s sim boundary. Once the link is gone, sim time stops advancing, so in production `step()` would never see the loss, unless an event callback that calls `snapshot` happens to run. Suggested fix, outside this task: have `OpcUaEquipmentPort.drain_events` call `self._check_lost()` once it has delivered the buffered events, or expose a public `lost` that `ReconnectingPort.drain_events` checks. The core already handles a raising `drain_events` (tested with a subclass).
2. A `ConnectionError` raised by `snapshot` in the middle of `dispatcher.tick()` aborts that tick partway. `Dispatcher._in_tick` is reset in its `finally`, but the order state may be partially updated. This is inherent to the frozen ECS; a later tick re-evaluates.
3. `status()`/`kpis()`/`equipment()` called from the API (Task 4) while bound to a lost OPC UA link will raise `ConnectionError` out of `snapshot`. Task 4 should catch it and call `core.on_link_lost()` (or the core could guard these calls).
