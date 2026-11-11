# Task 6 report: screenshot check, visual comparison, documentation

Status: DONE. Branch `feat/plan-b2b-viewer`, commit `53c8609` (docs: Plan B2b README section and viewer screenshot).

## What was built

- `tests/ui/render_check.py` (manual script, not collected by pytest): starts sim + gateway + ECS service (`auto_release=False`) on `tests/ui/faults_fast.yaml` via `opcua_procs`, runs `run_once_http` in a daemon thread (same pattern as the conftest fixtures). It waits until sim_time >= 60 s with >= 2 AGVs BUSY (max 60 s wall), then up to 30 s more for an alarm (optional), then opens `/ui/` at 1400x800, waits for `window.__viewer.ready` plus 1.5 s, takes the screenshot to argv[1] and exits 0. Exit 1 if the AGVs never work (the message includes the runner error), exit 2 on bad usage. It honours `VIEWER_BROWSER` (`--no-sandbox`). Playwright starts only after the helpers' `asyncio.run()` startup calls. Wall time is about 30 s.
  - Deviation from the brief: the brief said to use `terminal_tos_stub.submit`. I used the runner (`run_once_http`) instead, as the dispatch context asked. `submit` neither prefills nor starts the sim (see README B2a).
  - Choice: `faults_fast.yaml` instead of `baseline.yaml`, so that the alarm panel shows rows.
- `docs/superpowers/reports/plan-b2b/screenshot.png`: final output (sim t = 241 s, 2 NEEDS_RELEASE alarms).
- `serve.py`: one INFO line at startup, either `viewer at http://<host>:<port>/ui/` or `viewer package terminal_viewer absent; no /ui/ (install the 'viewer' extra)`. Verified by hand.
- README: new section "Webviewer (B2b)" after B2d. It covers opening the viewer, a shape table, state colours, the panel, both actions with their error codes (UNKNOWN_EQUIPMENT / RELEASE_REFUSED / UNKNOWN_JOB / 422 / 503), the `e2e_ui` marker with `playwright install chromium` and `VIEWER_BROWSER` as fallback, `render_check.py`, and the limitations (no auth, 1 Hz positions, `--auto-release` not needed but quarantine still needs a human).

## What the first screenshot showed (compared with the reference and spec §2)

1. The map was tiny and vertically centred: a 400x140 m world in a 1040x760 canvas, with large empty bands above and below.
2. The quay was a dark-blue band half a cell high that the lane AGVs overlapped. There was no berth/water side like the reference.
3. STS bars were filled in the state colour (green), not blue as in the reference. The STS id sat at the bottom right, on the hatched lane.
4. Blocks were drawn only 20 m deep (from the TP row to `worldH`), so they looked like thin strips. The TP cell was an unlabelled empty square.
5. AGV route lines were wrong: the AGV reports its whole planned route, including cells already passed, so the viewer drew a back-and-forth triangle.
6. AGV ids collided with each other and with STS bars (`AGV-02`/`AGV-06` and `AGV-04`/`AGV-03` at the lanes).
7. Panel: 24 KPI rows (raw English keys) pushed the alarms below the fold, so only 1 of 2 alarms was visible at 800 px. The legend had no state colours.

## What I changed (viewer static files only)

- `map.fit`: adds a 1-cell berth strip above y=0 (`worldTop`, newly returned). Blocks are drawn `BLOCK_DEPTH_CELLS = 2` deep. The map is top-aligned (oy = margin) rather than vertically centred. `worldH` still means the world bottom, and the existing tests are unchanged.
- The berth strip is light blue with a heavy dark-blue quay edge and a "Kade" label.
- STS: a dark-blue bar reaching into the berth strip, with an 18 px cabin in the state colour on the quay edge. A FAULT/OFFLINE STS is still obvious (OFFLINE has a dashed outline on the cabin). The id sits at the top right, in the berth strip. The hit-test rectangle covers the whole bar.
- Blocks: "Blok X" in 13 px semibold. The TP cell has a white fill and a "TP" label, hatched when occupied. The ASC bar spans the drawn block depth.
- AGV: `remainingRoute()` draws only the cells after the route cell nearest the AGV. Ids are placed after all bodies, above, or below when the spot above is taken by a neighbour's id. All map labels have a white halo and are 12 px.
- Panel order is now banner, Alarmen, KPI's, Selectie, Legenda, Events. KPIs use 12 px with Dutch labels (utilisation shown as %, `sim_elapsed_s` hidden as a duplicate of the sim time). The legend is a 2-column grid with the 5 state colours (OFFLINE dashed), "AGV met container", and berth/path/yard.

Final screenshot: quay on top with the berth strip, STS-01/02 bars with cabins and ids, hatched lanes, AGVs with ids and route, red FAULT AGVs (AGV-01, AGV-04 with a white box), blocks A–D with TP and ASC bars plus ids. The panel shows a green banner, both "Vrijgave nodig" alarms with a **Vrijgeven** button, and the KPI table. The selection, legend and events sections are below the fold and scroll.

## Verification

- `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -q`: 8 passed (107 s), run after the last JS change.
- `uv run pytest -q`: 540 passed, 14 deselected.
- `uv run ruff check tests/ui/render_check.py packages/ecs_service/src/terminal_ecs_service/serve.py`: clean. `ruff check tests/ui packages/ecs_service` still reports one pre-existing I001 in `ecs_service/port.py`, which I did not touch.

## Concerns / notes

- Spec §2 says "STS = blauwe balk" and also "toestandskleuren". I resolved this with a blue structure plus a state-coloured cabin; the README describes it.
- Spec §2 places the legend at the bottom of the panel. It is now above Events, because the 50-event log would otherwise bury it. It is still in the panel.
- About 250 px stays empty below the map at 1400x800 (the layout aspect is about 2.2:1). This is acceptable; a future option is to put the legend there.
- The block depth on screen (2 cells) is schematic, not the real 40 bays x 6.5 m, the same as the prototype's horizontal blocks.
