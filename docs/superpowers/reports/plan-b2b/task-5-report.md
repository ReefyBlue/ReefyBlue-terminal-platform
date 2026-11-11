# Task 5 report: live stream, polling, dimming on lost link

Status: DONE. Commit: c74848d `feat(viewer): WS event stream with reconnect and 1 Hz equipment polling` (branch feat/plan-b2b-viewer).

## What changed
- `viewer.js`
  - `stream.start(state, onMessage, onOpen)`: same-origin `ws(s)://host/events`. Messages are parsed and passed to `viewer.onMessage`, which applies them and refetches `/kpis` on `status`. `state.link.ws` follows the socket. On close the stream reconnects after 1 s, doubling to at most 10 s; the delay resets on open. `onOpen` runs on every (re)open.
  - `poll.refresh(state)` / `poll.start(state)`: `GET /equipment` on a `setTimeout` chain, every 1000 ms and 5000 ms after a failure. A 200 sets `link.ecs = true` and replaces the snapshot. A non-200 sets `link.ecs = false`.
  - `map.draw` dims (`globalAlpha 0.5`) when `link.ecs === false || link.connected === false`.
  - main: after the initial boot snapshots it starts the stream and the poll. Each (re)open resyncs `/alarms`, `/status`, `/kpis` **and `/equipment`**. `ready` is only set after the first WS open and its resync have finished.
  - `window.__viewer` now also exposes `stream` and `poll`.
- `tests/ui/conftest.py`: new module fixture `proxied` that yields `(url, proxy)`. It starts its own sim + gateway (`baseline.yaml`), puts a one-route `LinkProxy` in front of the gateway endpoint (the same arrangement as `test_blip_during_run.py` (b)), and points the ECS service at it with `auto_release=False`. The runner runs in a daemon thread. The processes run in their own thread, using the same pattern as `fault_agv`.
- `tests/ui/test_viewer_e2e.py`: the brief's `test_cut_shows_offline_and_dims_then_recovers`, adapted to `(page, proxied)` → `url, link = proxied`. The assertions are the brief's, unchanged.

## Deviations from the brief (with reasons)
1. **`GET /equipment` answers 200 during the cut, not 503.** I measured this during the cut: the `ResilientPort` serves its cache, which `unbind` has set to OFFLINE. So in this scenario the poll keeps `link.ecs = true`, and the dimming during the cut comes from `link.connected === false`. The 503 → `link.ecs=false` path is implemented but this test does not exercise it.
2. **The resync on open also refetches `/equipment`, and `ready` waits for the first resync.** Without this the full file failed in two ways:
   - The cut test failed when `link.cut()` ran right after `ready`, before the WS had registered on the server. The OFFLINE `equipment` messages were then lost. The alarm row did appear through the open-resync `/alarms`, but the states were still the old ones until the next poll.
   - `test_resolve_buttons_only_for_unknown_outcome` and `test_refused_action_shows_error_for_5_s` inject alarms with `model.apply` right after `ready`. The open-resync `/alarms` then overwrote those alarms.
3. **WS-vs-poll race guard.** A snapshot that was requested before a WS state change, and arrives after it, keeps the WS state for that device. This is tracked with `state.equipmentEpoch` and `state.stateEpoch: Map`. Positions and extra fields still update. Without the guard, a poll in flight at the moment of the cut could roll the OFFLINE states back to their old values.

## TDD evidence
- RED (before any JS change): `test_cut_shows_offline_and_dims_then_recovers` FAILED. The `wait_for_selector("#alarms li[data-key^='LINK_LOST|']")` timed out after 30 s because there was no WS.
- GREEN when the test ran on its own: PASSED.
- First full-file run after the minimal implementation: 2 failed then 3 failed (deviations 2 and 3 above). After the fixes:
  - Full `-m e2e_ui tests/ui` run 1: **6 passed** in 98.9 s
  - Full `-m e2e_ui tests/ui` run 2: **6 passed** in 99.8 s
  - The two full runs before the final per-device merge (an earlier "discard whole snapshot" guard): 6 passed and 6 passed.
- Default suite `uv run pytest -q`: **540 passed, 12 deselected**. This run was before the last JS-only refinement. After it, the viewer-related default tests passed (59 passed). `ruff check tests/ui` is clean.

## Concerns
- The brief's claim that `/equipment` gives 503 while the gateway link is down does not hold (see deviation 1). The service's 503 path happens only when an exception is raised during the request.
- `ready` now depends on the WS opening. If WS is unavailable, the map still draws but `ready` never becomes true. `ready` is a test hook only.
- The `proxied` fixture adds about 30 s of stack startup to the e2e_ui file.

## Fix report (review round 1)

Commit: b56442a `fix(viewer): WS alarm messages survive a concurrent /alarms refresh`.

- **IMPORTANT, alarm resync race**: `model.apply` now bumps `state.alarmEpoch` on every `alarm` message and appends it to `state.alarmLog` (the last 200 messages). `refreshAlarms` records the epoch before the request. When the response arrives it goes through `model.mergeAlarms(state, items, epoch)`: the snapshot is applied, then every logged alarm message newer than `epoch` is applied again on top. If the log no longer reaches back to `epoch`, the snapshot is dropped.
  - I chose merging over "drop the snapshot" or "refetch once". Merging keeps the gap resync after a reopen. A refetch would also wipe alarms that only came over the WS.
  - The guard covers every caller: the open-resync after any reopen, and the post-action refresh. `viewer.refreshAlarms` is exposed for the test.
- **(a)** The `proxied` docstring now says that during a cut `GET /equipment` answers 200 with every device OFFLINE.
- **(b)** The cut test also asserts `state.link.connected === false` during the cut. New test `test_equipment_503_marks_ecs_link_down_until_200`: it routes `/equipment` to 503 and checks that `link.ecs` goes false, then un-routes and checks that it goes true again.
- **(c)** The equipment branch of `model.apply` only bumps `equipmentEpoch` and sets `stateEpoch` when the item exists.
- New test `test_alarm_message_during_alarms_refresh_survives`: it holds back the `/alarms` response with `page.route`, injects a `NEEDS_RELEASE|XX-42|` alarm with `model.apply`, fulfills the held request with `[]`, and asserts that the alarm survives.
  - RED: with `mergeAlarms` temporarily reduced to a plain `setAlarms`, it fails with `assert 'NEEDS_RELEASE|XX-42|' in []`.
  - GREEN: it passes with the merge.
- Covering tests:
  - Whole `-m e2e_ui tests/ui` file, run twice: 8 passed (103.6 s) and 8 passed (105.7 s).
  - `uv run pytest -q`: 540 passed, 14 deselected.
  - `ruff check tests/ui`: clean.
