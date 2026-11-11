# Task 4 report: side panel (banner, KPIs, alarms with actions, event log, selection)

Status: DONE. Commit `9f20d21` on `feat/plan-b2b-viewer`.

## Implemented
- `api`: `request(method, path, body)` → non-2xx throws `ApiError{status, code, message}` with code = `body.code` ?? `body.detail` (string only) ?? `"HTTP <status>"`, message = `body.message` ?? `""`. `api.{layout, equipment, alarms, status, kpis, release(id), resolve(jobId, outcome)}`. `getJson` kept (now via `request`). `errorText(e)` = `code: message` (just `code` without a message).
- `model` (no DOM): the Task 3 `apply` stub replaced. `alarmKey(a)` = `kind|equipment_id|job_id-or-""`; `apply` handles `alarm raised|cleared` (by key; list sorted oldest first by `since_sim_time`, then key), `status` (→ `setStatus`; `connected` boolean also sets `link.connected`), `equipment` (event + `new_state` onto the item if present), `job` (event), `link` (`link.connected`, `link.since` = sim_time of the drop). Also `setAlarms`, `setStatus`, `setKpis`, `setAction`, `select`. Events newest first, ring 50. Per-button action state lives in `state.actions` (`{busy, error, until}`), so no hidden globals; state stays structuredClone-able.
- `panel.render(state)`: `#banner` (grey "Viewer ↔ ECS verbroken" when `!ws && !ecs`, red "Gateway verbroken" [+ " sinds t=… s" when the drop time is known] when `connected === false`, else green "ECS ↔ gateway verbonden"); `#kpis` (sim time, done/failed/open/offline/needs_release/unknown_outcomes with Dutch labels, then every `/kpis` entry); `#alarms` (Dutch labels, `Vrijgeven` once per equipment on its first row, `Voltooid`/`Mislukt` only for UNKNOWN_OUTCOME, error lines, null job ids omitted); `#events`; `#selection` (id, kind, state, job, extra). Each section's innerHTML is rewritten only when its HTML changes, so buttons are not replaced on unrelated re-renders.
- `main`: exposes `panel`, `api`, `onMessage` (apply + `/kpis` refetch on `status`, for Task 5's stream) on `window.__viewer`; awaits initial `GET /alarms`, `/status`, `/kpis` before `ready` (so a test's injected alarms cannot be overwritten by a late bootstrap fetch); click delegation on `#alarms` (disable while busy → API → on success clear and refetch `/alarms` + `/status`; on error show `code: message` for 5 s, button re-enabled immediately); canvas click → `map.hitTest` → `model.select`.
- `index.html`: panel skeleton (`#banner`, `#kpis`, `#alarms`, `#selection`, `#events`, legend "Kade (berth)", "Rijpaden (internal path)", "Stack (yard)"). `viewer.css`: panel styles; empty lists show placeholder text via `:empty::before` (no `li`, so `#alarms` stays empty for later tests).

## Tests
- `tests/ui/faults_fast.yaml` per controller ruling (60 containers, AGV mtbf 600/mttr 120, STS/ASC off, speed 50, 7200 s).
- `tests/ui/conftest.py::fault_agv` (module scope): own sim+gateway+ECS (`auto_release=False`), runner `run_once_http` in a daemon thread, yields the URL once `GET /alarms` has a `NEEDS_RELEASE` for an AGV (≤ 90 s). The process lifetime runs in a separate thread: when `browser` was already started by an earlier test, `asyncio.run()` in the helpers fails in the main thread (observed: `RuntimeError: asyncio.run() cannot be called from a running event loop`). Teardown: stop event → contexts exit (SIGTERM as in the existing fixtures).
- `test_panel_shows_alarms_and_release_works(fault_agv, page)`: brief test adapted per ruling: waits for the first `NEEDS_RELEASE|AGV-` row, takes the id from `data-key`, same assertions, click loop up to 30 s while the row exists and the button is enabled, then waits for 0 rows.
- `test_resolve_buttons_only_for_unknown_outcome`: verbatim from the brief.
- Added (beyond brief, coverage of spec §5 / selection): `test_refused_action_shows_error_for_5_s` (release on an injected `XX-99` alarm → row shows `UNKNOWN_EQUIPMENT: …`, button enabled, error gone within 7 s), `test_canvas_click_selects_equipment` (click on STS-01 → `#selection` shows STS-01/STS/IDLE). Both written after the implementation (no RED).

## TDD evidence
- RED (feature missing): `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -v` → `test_panel_shows_alarms_and_release_works FAILED (Page.wait_for_selector: Timeout 60000ms … NEEDS_RELEASE|AGV-)`, `test_resolve_buttons_only_for_unknown_outcome FAILED (Timeout 30000ms … UNKNOWN_OUTCOME|AGV-02|J-7)`, `2 failed, 1 passed in 144.91s`. (An earlier RED run showed the fixture-order RuntimeError above, fixed with the thread.)
- GREEN: same command → `3 passed in 55.79s`; final (5 tests) → `5 passed in 67.73s`.
- Default suite: `uv run pytest -q` → `540 passed, 11 deselected in 268.73s` (run before two last edits: the JS-only `errorText` refactor and ruff's removal of an unused `# noqa: E402` in `tests/ui/conftest.py`; e2e_ui re-run green after them).
- In the release test run, the ECS log showed `POST /equipment/AGV-01/release 200` on the first click (AGV already repaired when the page loaded), so the 409 retry branch was not exercised there; the error path is covered by the `XX-99` test (404).
- Manual scratch check (not committed): no console errors; banner "Gateway verbroken sinds t=14.0 s" after a `link` message; event log and `new_state` applied; screenshot looked right.

## Files changed
- packages/viewer/src/terminal_viewer/static/{viewer.js, index.html, viewer.css}
- tests/ui/{conftest.py, test_viewer_e2e.py, faults_fast.yaml (new)}

## Self-review
- `model` touches only its arguments; `panel` touches only the DOM; transient UI state is in `state.actions`.
- After a successful action the viewer refetches `/alarms` + `/status` (no WS until Task 5). This replaces the alarm list; once the stream exists the two may briefly race, but the refetch is the server's current truth.
- The banner adds " sinds t=… s" to "Gateway verbroken" (spec §5 "gateway verbroken sinds …"); Task 5's test checks only "verbroken".

## Concerns
1. Commit trailer: the system attribution reminder in this session named a different Co-Authored-By line than the controller/global-constraints (`Claude Fable 5.1`). I followed the project's global constraints and existing history (Fable 5.1). Amend if the other line was intended.
2. `fault_agv` depends on timing: an AGV must fault within 90 s wall (expected after a few seconds at speed 50 with 6 AGVs × mtbf 600 s). Once Task 5 adds the WS stream, the release test could become flaky if the same AGV faults again (new NEEDS_RELEASE row) before the test sees zero rows (mtbf 600 s sim ≈ 12 s wall mean per AGV). That can't happen yet because without a stream the page only refetches alarms after an action. It passed in all 4 runs.
3. Two tests added beyond the brief (error display, selection).

## Fix report (review round 1)

What changed:
- Important: `alarmsHtml` (viewer.js) now puts `Vrijgeven` only on the first `NEEDS_RELEASE` or `LINK_LOST` row of each equipment (`RELEASABLE` set). An `UNKNOWN_OUTCOME` row never gets a release button, so its button and error can't end up on the wrong row. `test_resolve_buttons_only_for_unknown_outcome` now also asserts `button.release[data-equipment='AGV-02']` count == 0.
- (a) The release e2e test now matches rows with `NEEDS_RELEASE|{eid}|` instead of every alarm for that equipment.
- (b) `ApiError` passes `message` straight to `super(message)` and keeps `code` separate. It no longer reassigns `this.message`. `errorText` still renders `code: message`.

RED: the extended resolve test against the previous viewer.js (`git show HEAD:…`) failed with `assert 1 == 0` (release button count for AGV-02).

Covering tests:
- `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -q` → `5 passed in 59.76s`
- `uv run pytest -q` → `540 passed, 11 deselected in 260.45s`
