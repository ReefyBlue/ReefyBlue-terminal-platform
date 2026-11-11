# Task 3 report — derived grid view and the sim on a v2 layout

**Status:** DONE
**Branch:** feat/plan-c1-terminal-design
**Commit:** 31bc991 `feat(sim): v2 layouts load through a derived 20 m grid; mv_small baseline fixture`

## Files
- New: `packages/model/src/terminal_model/layout_grid.py` (`Cell`, `GridSts`, `GridBlock`, `GridView`, `cell_of`, `grid_view`)
- Changed: `packages/sim/src/terminal_sim/layout.py` (`load_layout`: `is_v2` → `_from_v2` = `load_terminal` + `grid_view` → legacy `Layout`; v1 path untouched)
- Tests: `tests/model/test_layout_grid.py` (8), `tests/sim/test_layout_v2_sim.py` (2), `tests/e2e/test_mv_small_baseline.py` (1, marker `e2e`)
- Fixture: `tests/e2e/fixtures/mv_small_baseline_seed1.json`

## Derivation rules (as implemented)
- `cols = ceil(quay/20)`, `rows = ceil(depth/20)` → small 20×23, demo 60×23.
- STS lane cells: cells of the lane centres (`x_m`, `y_m`) in lane order, deduplicated, first two; if only one distinct cell, add its east neighbour (west neighbour at the east edge). Small: STS-01 (3,2)/(4,2), STS-02 (9,2)/(10,2), STS-03 (16,2)/(17,2).
- `GridSts.x_m = x0_m`, `y_m = sts_rails.y_ws_m` (10) — used for the legacy `StsSpec.position` (my choice; legacy used y 0).
- `tp_ws_cell` = cell of rack 1; `tp_ws_m` = rack 1 (x, y); `asc_id` = `asc_ws_id`. Small TP cells (1,6)/(3,6)/(5,6)/(7,6); demo: 24 distinct.
- `agv_start_cells`: buffers sorted by STS order (stable), buffer cell or nearest free cell on the same row (east before west), never a handover cell. Small: 12 distinct cells on row 4.
- `LayoutError` on: lane cell shared between STSs, TP cell shared with a lane cell or another TP, any cell outside the grid, STS without lanes, zone without racks, no free start cell.

## Evidence
- RED: new tests failed at collection (`ModuleNotFoundError: terminal_model.layout_grid`).
- GREEN: `tests/model/test_layout_grid.py tests/sim/test_layout_v2_sim.py` 10 passed.
- `uv run pytest tests/model tests/sim -q` → 187 passed.
- `uv run pytest tests/e2e/test_mv_small_baseline.py -q` → passed on first run (fixture written) and on 2 further runs (exact match). 200 done, 0 failed, 0 stuck, not timed out; 90.2 moves/h total (~30/STS), turnaround 7665 s.
- `uv run pytest -q` → 681 passed, 17 deselected.
- `uv run pytest tests/e2e/test_baseline.py tests/e2e/test_service_equivalence.py tests/e2e/test_faults_equivalence.py -q` → 4 passed (legacy fixtures exact).
- ruff clean on touched files (fixed one import-order issue in `terminal_sim/layout.py`).

## Concerns / deviations
1. **Lane-cell rule differs slightly from the controller's wording.** I followed the global constraint (dedupe lane cells, take the first two) with the "east neighbour" fallback only when all lanes share one cell. For STS-02 (lanes at 196.5/200/203.5) this gives (9,2)/(10,2), whereas "centre-lane cell + east neighbour" would give (10,2)/(11,2). Both are distinct; easy to switch if the controller prefers the strict rule.
2. **`load_scenario` caps `n_sts ≤ 2`, `n_agv ≤ 6`.** A `tests/e2e/fixtures/mv_small_baseline.yaml` scenario file would be rejected, so the test builds the scenario with `dataclasses.replace`. Task 5 (or whoever needs a reusable scenario file) must lift those caps.
3. `World` still hard-codes `STAGING_ROWS = (2, 3)`: on the v2 grid idle AGVs park in the lane row (minus lane cells) and the drive-lane row, not in the buffers (row 4). Works (baseline completes), but not yet geometrically faithful.
4. Small layout blocks span x 25–180 only while STS-03 sits at x 333, so its AGVs drive far; this is a property of the generated layout, not the grid.
5. The fixture test writes the file when it is missing (per brief); a deleted fixture silently regenerates.
