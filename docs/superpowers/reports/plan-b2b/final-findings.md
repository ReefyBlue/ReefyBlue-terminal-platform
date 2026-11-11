# Final review findings — fix wave (all four; plus minors 1 and 4 which are cheap and touch the same code)

Important 1 — viewer.js draw loop (`frame()`): schedule `requestAnimationFrame(frame)` FIRST (or try/finally), wrap `map.draw`+`panel.render` in try/catch that `console.error`s the exception and keeps the loop alive. Add an e2e test: via page.evaluate, put an equipment item with `extra.route = [{x:1,y:1}]` (object cells, not arrays) into `state.equipment`, set `dirty`, wait 2 frames, then change a device state and assert the panel/map still update (e.g. `ready` stays true and a subsequently injected alarm row appears).

Important 2 — tests/ui/test_viewer_e2e.py Review Focus 1: the current assertion (`worldW >= 400 && worldH >= 120`) is satisfied by a grid-only fit. Add: `v.map.fit({quay_length_m:100, grid_cell_m:20, grid_cols:5, grid_rows:2, sts:[{equipment_id:'S', x:900, y:0, lane_cells:[[0,0],[0,0]]}], blocks:[], agv_start_cells:[]}, 1000, 600)` → assert `worldW >= 920`.

Important 3 — Review Focus 4: inject `LINK_LOST|AGV-09|` and `NEEDS_RELEASE|AGV-09|` via `model.apply`; assert two `#alarms li[data-key*='|AGV-09|']` rows and exactly one `button.release[data-equipment='AGV-09']`.

Important 4 — tests/test_architecture.py: (a) AST-scan `packages/ecs_service/src/terminal_ecs_service/**/*.py` and assert the only `terminal_viewer` import is inside function `_viewer_dir` in app.py (serve.py currently imports `_viewer_dir` from app — that is fine; it must not import terminal_viewer itself); (b) assert the three static files contain no `http://`, `https://` or protocol-relative `//` URL (except inside comments is NOT an exception — none at all).

Minor 1 — WS liveness: when `link.ecs` transitions false→true, call `refreshAlarms()` and `refreshStatus()` (and refresh kpis) so a half-dead socket does not leave stale alarms; one-line in the poll success path.

Minor 4 — `refreshKpis`: add an in-flight guard (skip if a request is already out).

Covering tests: `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -q` and `uv run pytest -q` (architecture test is in the default suite). Commit with trailer lines:
Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011Q6oL3tzJGQycNuMVuzTtk
