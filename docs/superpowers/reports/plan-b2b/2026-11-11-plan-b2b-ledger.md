# SDD ledger — plan: docs/superpowers/plans/2026-11-11-plan-b2b-viewer.md
Spec: docs/superpowers/specs/2026-11-11-plan-b2b-viewer-design.md. Branch feat/plan-b2b-viewer, base feat/plan-b2d-link-loss (merge-base 1dd910e is plan commit; code base = fea9769).

## Pre-flight scan
| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T1 → T3 | `/layout` JSON fields ↔ `map.fit(layout)` reads quay_length_m, grid_*, sts[].x/y, blocks[].tp_ws, bays | match |
| T2 → T3/4/5 | `/ui/` serves `viewer.js`, `window.__viewer` ↔ tests evaluate it | match |
| T3 → T4 | `model.apply`, `state.alarms`, `alarmKey` | T3 defines state shape only; T4 defines apply/alarmKey — consistent (T4 brief carries both) |
| T3 → T5 | `model.setEquipment`, `state.link.{ws,ecs,connected}` | match |
| T1 vs existing tests | `LayoutLike` protocol gains attributes; existing fixtures use SimpleNamespace without them | T1 says only `/layout` needs them; EcsCore ctor must not touch new attrs → ok |
| T5 test | expects `every(e.state==='OFFLINE')` during cut while `/equipment` is 503 | relies on WS `equipment` messages carrying `new_state: OFFLINE` for every device (B2d ReconnectingPort emits STATE_CHANGED for all) → ok |
| Self-consistency | T2 mounts `/ui` after routes; `/` redirect — `test_without_viewer_package_no_ui` monkeypatches `_viewer_dir` before `create_app` → ok (must be module-level function, looked up at call time) |
Ruling: pre-flight clean — no conflicts — cost if wrong: a task review catches it.

Task 1: minor (deferred): /layout tests check only first sts/block, not list lengths.
Task 1: complete (commits 1dd910e..7e7da69, review clean; trailer + branch verified by controller)
Ruling: Playwright's chromium download is blocked by the workspace network allowlist; `/usr/bin/google-chrome` (Chrome for Testing 153) is present and Playwright 1.65 drives it via `executable_path`. tests/ui/conftest.py reads env `VIEWER_BROWSER` (executable path; adds `--no-sandbox`) and falls back to Playwright's bundled chromium when unset. Documented in README. — cost if wrong: e2e_ui unrunnable on a machine without either browser (test skips with a clear message rather than failing).
Task 2: minor (deferred): real ImportError branch of _viewer_dir untested; bare /ui redirect untested.
Task 2: complete (commits 7e7da69..a0a6103, review clean)
Task 3: Ruling: implementer fixed Task-1 gap outside brief — `serve.load_layout` dropped geometry so real `GET /layout` 500'd (7dec4c9, with regression test). Accepted: the endpoint is the task's dependency. — cost if wrong: none visible.
Task 3: Ruling: ASC gantry bar scaled to drawn block width `min(bays*6.5 m, 0.8*block spacing)` instead of literal 260 m (blocks would overlap in terminal.yaml's 100 m spacing). — cost if wrong: ASC position not metre-accurate on the map; cosmetic.
Task 3: minor (deferred): rAF loop dies if map.draw throws (schedule rAF first / try-finally); NaN gantry_position_m hides ASC (Number.isFinite guard); hitTest/dimming only hand-checked; conftest sys.path.insert for opcua_procs. ⚠️ resolved: only terminal.yaml goes through serve.load_layout.
Task 3: complete (commits a0a6103..9f7b23e, review clean)
Task 4: Ruling: `fault_agv` fixture = own sim+gateway+service with tests/ui/faults_fast.yaml (agv mtbf 600/mttr 120, speed 50) + runner thread (as test_blip_during_run) so the clock runs; test waits for the first NEEDS_RELEASE|AGV-* row instead of hard-coded AGV-01; release click tolerant of 409 while still FAULT. — cost if wrong: flaky e2e_ui test (deselected from default suite).
Task 4: fix round 1/5 (3 addressed, 0 open — release button limited to NEEDS_RELEASE/LINK_LOST rows; test selector; ApiError super(message); commits 9f20d21..a87637c)
Task 4: minor (deferred): put() caches last HTML in data-html attribute (use WeakMap); selection renders null extra values as "null"; conftest teardown join(60) silent on hang.
Task 4: ⚠️ for Task 5: stream must dispatch through `viewer.onMessage` (status→/kpis refetch), not model.apply directly; poll must set link.ecs false/true.
Task 4: complete (commits 9f7b23e..a87637c, review clean after 1 fix round)
Task 5: Ruling: plan's claim "GET /equipment answers 503 during a gateway cut" was wrong (service answers 200 with all OFFLINE); dimming on `link.connected === false` covers the cut; `link.ecs=false` path remains for viewer↔ECS loss. — cost if wrong: none; both paths implemented.
Task 5: fix round 1/5 (4 addressed, 0 open — alarm resync replay-log guard; docstring; connected/ecs tests; epoch guard; commits c74848d..b56442a)
Task 5: minor (deferred): mergeAlarms returning false (log overrun >200) never retried; no test for a `cleared` arriving mid-refresh; `ready` requires WS open (documented deviation).
Task 5: complete (commits a87637c..b56442a, review clean after 1 fix round)
Task 6: Ruling: screenshot script uses runner (`run_once_http`) + faults_fast.yaml instead of brief's `terminal_tos_stub.submit` — clock must run and an alarm row is informative. — cost if wrong: none. Controller viewed screenshot.png: matches reference orientation/style; AGV labels crowd in the STS lane; ~250 px empty below blocks (blocks 2 cells deep) → deferred minors for final review.
Task 6: minor (deferred): duplicate viewer startup log (serve.py + app.py; serve imports private _viewer_dir); logged URL shows 0.0.0.0; README "FAULT hier afgehandeld" slightly loose; bare except in render_check runner thread.
Task 6: complete (commits b56442a..53c8609, review clean)
Final review (opus): 0 Critical, 4 Important (rAF loop dies on throw; RF1 test non-discriminating; RF4 untested; _viewer_dir import rule + no-CDN untested), 5 Minor. One fix wave dispatched with the 4 Important + minors 1 (WS liveness resync) and 4 (kpis in-flight guard). Minor 5 (CSRF on POST release/resolve, WS Origin) → B2c risk list.
Final fix wave: commit f43ad0b (trailer amended by controller). Re-review: all 6 addressed, no new Critical/Important. Residual minors (parked, ruling: not merge-blocking): draw-error test accepts any console error; kpis guard may serve one stale response; architecture guards narrow (function name only, no dynamic imports, URL regex misses upper-case/ws://); fault_agv fixture setup flake seen once (passed on re-run) → B2c flakiness list.
Ruling: CSRF on POST release/resolve and no Origin check on WS /events (pre-existing B2a surface, home-network only) → recorded for B2c risk list, not fixed here. — cost if wrong: a malicious page on the home LAN could release a device.
