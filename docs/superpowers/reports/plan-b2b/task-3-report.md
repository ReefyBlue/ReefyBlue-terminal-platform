# Task 3 report — map rendering, page skeleton, first e2e_ui test

Status: DONE_WITH_CONCERNS (one out-of-brief fix to Task 1 code, see Concerns 1)

## Commits
- `7dec4c9` fix(ecs_service): service layout loader carries the geometry GET /layout serves
- `9f7b23e` feat(viewer): canvas top view of quay, STS, AGV and blocks from /layout and /equipment

## Implemented
- Root `pyproject.toml`: marker `e2e_ui` (text verbatim from the brief), `addopts = "-m 'not e2e_opcua and not e2e_ui'"`, dev dep `playwright>=1.47` (resolved 1.65.0, `uv.lock` committed). No pytest-playwright.
- `tests/ui/conftest.py`: `viewer_url` (module scope; `sim_and_gateway(scenarios/baseline.yaml, n_sts=2, n_agv=6)` + `ecs_service(..., auto_release=False)`), `browser` (module scope; `VIEWER_BROWSER` -> `executable_path` + `--no-sandbox`, else bundled chromium; launch `Error` -> `pytest.skip` naming `VIEWER_BROWSER` and `uv run playwright install chromium`), `page` (fresh context, 1400x800). `opcua_procs` imported via a `sys.path` insert of `tests/e2e`.
- `tests/ui/test_viewer_e2e.py`: the brief's test verbatim (plus module docstring).
- `index.html`: header "Terminal viewer", `<main><canvas id="map"><aside id="panel">`, empty data: favicon (avoids a 404 console error).
- `viewer.css`: column flex body, two-column grid `minmax(0,1fr) 360px`, system font, white background.
- `viewer.js` (sections `api`, `model`, `map`, `main`; Tasks 4-5 add `stream`/`poll`/`panel`):
  - `COLORS` exported verbatim.
  - `model.initial()` returns the brief's state shape; `model.setEquipment` replaces the Map and sets `dirty`; `model.apply` is a stub that only sets `dirty` (message handling is Task 4's brief).
  - `map.fit`: worldW/worldH exactly per brief formula; uniform scale with 20 px margin, centred.
  - `map.draw`: light grid, quay band y∈[0, cell/2] `#1e5aa8`, blocks from `tp_ws.y` to worldH with centred letter, TP cell (hatched when `ws_tp_occupied`), STS 12 px bar from y=0 to bottom of lane row with id, lane cells hatched when `handover_lane_occupied`, ASC bar across the block, AGV 8x4 m rect with id above, white inner square when `loaded_container_id`, polyline AGV -> route cell centres. Fill `COLORS[state]` (grey for unknown/missing items), OFFLINE dashed outline, `globalAlpha 0.5` when `link.ecs === false`. Missing `extra` keys tolerated.
  - `map.hitTest`: screen rects (+3 px), topmost first AGV > ASC > STS.
  - Bootstrap: `/layout` then `/equipment`, each retried every 5 s (notice text in canvas while no layout; `link.ecs=false` while /equipment fails), then `link.ecs=true`; rAF loop draws only when `dirty`, DPR-aware canvas with resize handling.
- Out-of-brief: `terminal_ecs_service/serve.py` `Layout`/`load_layout` now carry quay/grid/STS/block geometry (+ `Position`, `Sts` dataclasses); regression test `tests/ecs_service/test_layout_and_ui.py::test_layout_from_service_loader_matches_sim_loader`.

## Deviations / interpretations
- ASC x position: the brief's `tp_ws.x - blockW/2 + gantry_position_m` with `blockW = bays*6.5 = 260 m` would overlap neighbouring blocks (100 m apart) and clamp most positions to the edges. Drawn block width = `min(bays*6.5, 0.8 * min block spacing)` (80 m here) and the gantry position is scaled onto it: `left + clamp(gp, 0, bays*6.5) * drawW/(bays*6.5)`, clamped to the block. Same formula, scaled.
- `ready` becomes true after the first draw once both `/layout` and `/equipment` have loaded (layout-only draws while /equipment is retried do not set it), so the test's `equipment.size == 12` check cannot race.
- `browser` fixture depends on `viewer_url`: the process helpers call `asyncio.run()`, which fails while Playwright's sync API holds the thread's running loop (seen as RuntimeError on the first run). Consequence: the skip (no browser) happens only after the processes started (~25 s).

## Tests and TDD evidence
- RED 1 (fixture order): `RuntimeError: asyncio.run() cannot be called from a running event loop` -> fixed fixture order.
- RED 2 (feature missing, placeholder viewer.js): `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -v` -> `FAILED ... Page.wait_for_function: Timeout 15000ms exceeded.`
- After implementing viewer.js still timed out; browser debug showed `GET /layout` -> 500, ECS log `AttributeError: 'Layout' object has no attribute 'quay_length_m'` (serve.py loader). RED 3: new unit test failed with the same AttributeError; after the fix `uv run pytest tests/ecs_service -q` -> `83 passed`.
- GREEN: `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -v` -> `test_map_draws_layout_and_fleet PASSED`, `1 passed in 27.86s`.
- Skip path: `uv run pytest -m e2e_ui tests/ui -rs` (no VIEWER_BROWSER, no bundled chromium) -> `SKIPPED [1] ... no browser for the viewer tests (BrowserType.launch: Executable doesn't exist ...); set VIEWER_BROWSER ... or run 'uv run playwright install chromium'`.
- Default suite: `uv run pytest -q` -> `540 passed, 7 deselected in 282.65s`.
- Manual visual check (scratch script, screenshots not committed): live fleet renders (2 STS, 6 AGV, 4 blocks+ASC); synthetic states verified BUSY/FAULT/OFFLINE(dashed)/MAINTENANCE colours, hatched lane and TP, route polyline, loaded box, ASC clamp at block edge; `hitTest` -> AGV-02, STS-01, null as expected.

## Files changed
- pyproject.toml, uv.lock
- packages/viewer/src/terminal_viewer/static/{index.html,viewer.css,viewer.js}
- tests/ui/conftest.py, tests/ui/test_viewer_e2e.py (new)
- packages/ecs_service/src/terminal_ecs_service/serve.py, tests/ecs_service/test_layout_and_ui.py

## Self-review
- Interfaces on `window.__viewer` match the brief names exactly; `state` stays structuredClone-able.
- `model`/`map` touch only their arguments and `ctx` (no document/window).
- YAGNI: no selection highlight, no panel content, `apply` stubbed — left for Task 4.
- Pristine output: no console errors on the page after the favicon fix.

## Concerns
1. Fixed a Task 1 defect outside this brief's file list (service's own layout loader lacked the geometry, so `/layout` 500'd in the real process; Task 1's unit tests used the sim loader). Reviewer should confirm this is acceptable here.
2. ASC/block width interpretation above (scaled gantry) differs from a literal reading of the brief.
