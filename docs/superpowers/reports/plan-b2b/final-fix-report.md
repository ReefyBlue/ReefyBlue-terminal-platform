# Final-review fix report — Plan B2b viewer

Branch `feat/plan-b2b-viewer`, commit `f376a23` (one commit). Paths below are relative to the repo root.

## Important 1: the render loop dies on a draw error
- **Change:** `packages/viewer/src/terminal_viewer/static/viewer.js:812` `frame()` now books `requestAnimationFrame(frame)` first. `map.draw`/`drawNotice` and `panel.render` each run in their own try/catch that calls `console.error`. After a failed draw the context is reset with `canvas.width = canvas.width`, because a throw inside a nested `save()` (hatch/label) would leave the save stack unbalanced. A plain `restore()` would not handle that.
- **Test:** `tests/ui/test_viewer_e2e.py:73` `test_draw_error_keeps_render_loop_alive`. It injects AGV-77 with `extra.route=[{x:1,y:1}]` and waits two frames. It then injects a `NEEDS_RELEASE|XX-77|` alarm and asserts three things: the row appears, `ready` is still true, and a console error was logged.
- **Red before the fix:** `Page.wait_for_selector: Timeout 5000ms exceeded … NEEDS_RELEASE|XX-77|` (1 failed, 2 passed).

## Important 2: the fit test was satisfied by a grid-only fit
- **Test:** `tests/ui/test_viewer_e2e.py:18`, inside `test_map_draws_layout_and_fleet`. It runs `map.fit` on the synthetic layout (STS at x=900) and asserts `worldW >= 920`. The STS was given `y:0` because the real `/layout` STS carries `y`.
- **Result:** it **passes on the current code**. `fit()` already puts STS x into `worldW`, which gives 920; a grid-only fit would give 100. So this is a regression guard, not a red test. The brief expected it to fail on the current code; it does not. No code change.

## Important 3: one release button per equipment
- **Test:** `tests/ui/test_viewer_e2e.py:61` `test_one_release_button_per_equipment`. It applies `LINK_LOST|AGV-09|` and `NEEDS_RELEASE|AGV-09|` and asserts two `li[data-key*='|AGV-09|']` rows and exactly one `button.release[data-equipment='AGV-09']`.
- **Result:** it **passes on the current code**. `alarmsHtml` already dedupes with its `released` set. The brief expected a failure here too; this is a guard. No code change.

## Important 4: architecture guards (pass on the current code, as expected)
- (a) `tests/test_architecture.py:170` `test_ecs_service_imports_viewer_only_in_viewer_dir`. It AST-scans `packages/ecs_service/src/terminal_ecs_service/**/*.py` and flags any `terminal_viewer` import that is not inside a function named `_viewer_dir`. `serve.py` importing `_viewer_dir` from `app` is allowed. A non-vacuity check confirms `app.py` mentions `terminal_viewer`. The self-test at `:179` confirms that a module-level import is flagged and the one inside `_viewer_dir` is not.
- (b) `tests/test_architecture.py:188` `test_viewer_static_files_reference_no_external_url`. It scans `index.html`, `viewer.js` and `viewer.css` line by line, comments included, with the `_URL` pattern at `:185` (`https?://` or a protocol-relative `//host.tld`). A plain `// comment` is not a URL and is not matched. The self-test at `:200` checks the pattern on `https`, `http`, `//host` and a URL inside a comment.

## Minor 1: resync when the ECS link comes back
- **Change:** `viewer.js:150` adds `poll.start(state, onRecover)`, which calls `onRecover()` when `state.link.ecs` goes from false to true. `viewer.js:921` wires it to `refreshAlarms()`, `refreshStatus()` and `refreshKpis()`.
- **Test:** `tests/ui/test_viewer_e2e.py:133` (lines 138–144 extended). After the 503 is lifted, a `GET /alarms` must be seen within 5 s.
- **Red without the fix:** `FAILED …test_equipment_503_marks_ecs_link_down_until_200` (viewer.js stashed).

## Minor 4: in-flight guard on refreshKpis
- **Change:** `viewer.js:844`. A `kpisInFlight` promise means a second call returns the request that is already out.
- **Test:** none. `refreshKpis` is not exposed on `window.__viewer`, and the covering e2e tests (status messages, initial load) still pass.

## Verification
- `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -q` → `10 passed in 92.31s`.
  - The first full run gave `9 passed, 1 error`: a setup error in the `fault_agv` fixture for `test_panel_shows_alarms_and_release_works`.
  - That test then passed on its own (`1 passed`), and the next full run passed 10/10. This points to flakiness in the fault-injection fixture, not to these changes.
- `uv run pytest -q` → `547 passed, 16 deselected in 241.18s`.
- `uv run ruff check tests/test_architecture.py tests/ui/test_viewer_e2e.py` → `All checks passed!`.
  - `uv run ruff check tests packages/ecs_service packages/viewer` as a whole reports 36 errors, all in files this commit does not touch (`port.py`, `tests/kpi`, `tests/sim` and others). The count is the same with these changes stashed.

## Notes
- Commit trailer: the findings file names a different Co-Authored-By model line. The session's attribution instruction was used instead (Claude Opus 5.5 plus the session link).
