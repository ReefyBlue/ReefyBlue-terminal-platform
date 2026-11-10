# Task 3 report: Sim-servers zenden events

## What
- `packages/sim/src/terminal_sim/opcua_servers.py`: `EventEmitter` now imported from `terminal_opcua.events`; `start()` uses `await EventEmitter.create(server, types.events, self._obj, equipment_id)` after `create_equipment_object`, before `server.start()`. `sync()` unchanged (flushes `_pending` via `emit`).
- `tests/sim/test_opcua_servers.py`: three event tests rewritten with `EventSink` on `rig.client` (`test_assign_via_opcua_runs_job`, `test_two_events_one_step_both_delivered` with consecutive seqs, `test_events_delivered_in_order` with J1,J2,J3). `event_from_json` import removed.
- `pyproject.toml`: added `tests/opcua`, `tests/gateway` to pytest `pythonpath` (per controller).

## TDD evidence
- RED: source reverted, `uv run pytest tests/sim/test_opcua_servers.py -q` -> collection ImportError (`EventEmitter` from `terminal_opcua.nodeset`). (Tests and source were written together, so RED was verified by reverting the source change.)
- GREEN: `uv run pytest tests/sim/test_opcua_servers.py -q` -> 5 passed. `uv run pytest tests/sim tests/opcua/test_events.py tests/opcua/test_nodeset.py -q` -> 132 passed.

## Self-review
Matches brief; no extras. Tests assert real delivery over the OPC UA subscription. Output pristine (only uv's UV_NATIVE_TLS warning).

## Concerns
None.
