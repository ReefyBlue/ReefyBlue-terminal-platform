# Task 2 report — builder and the two shipped layouts

Status: DONE. Commit `3994af3` on `feat/plan-c1-terminal-design`.

## Files
- `packages/model/src/terminal_model/layout_builder.py` (new): `standard_terminal(...)` (signature per brief), `to_yaml_dict(t)`. Geometry decisions are documented in the module docstring.
- `packages/model/src/terminal_model/layout.py` (changed): `TruckHead.len_m: float = 0.0` (optional YAML key), `depth_m` uses `y_m + len_m`; `validate` enum checks for `DriveLane.direction ∈ {E,W}`, `Rack.size ∈ {20,40,45}`, `Block.maintenance_lane_side ∈ {E,W}` (constants `DRIVE_DIRECTIONS`, `RACK_SIZES`, `LANE_SIDES`).
- `scripts/gen_layouts.py` (new): writes `terminal_mv_small.yaml` and `terminal_mv.yaml` (header comment + `yaml.safe_dump(d, sort_keys=False)`); idempotent (md5 unchanged on re-run).
- `terminal_mv_small.yaml` (665 lines), `terminal_mv.yaml` (1952 lines).
- `tests/model/test_layout_builder.py` (new, 9 tests): small/demo counts, demo spec values, zone coverage (all 16 `ZONE_TYPES`), YAML roundtrip, deterministic/plain types, shipped files == builder (parametrised ×2), enum validation.

## TDD evidence
- RED: `ModuleNotFoundError: No module named 'terminal_model.layout_builder'` (collection error).
- After implementing: 7 passed, 2 failed (`test_shipped_files_match_builder` — files absent). After `uv run python scripts/gen_layouts.py`: all pass.
- `uv run pytest tests/model tests/test_architecture.py -q` → 93 passed. Full `uv run pytest -q` → 669 passed, 17 deselected.
- ruff clean on touched files.

## Deviations / decisions
1. **depth 560 vs truck heads "y 525"** — the brief asks both `truck heads y 525` and `test_demo_counts … depth 560` (and Task 3 expects rows 28 = ceil(560/20)), but Task 1's `depth_m` was `max(block y1, truck head y_m)` = 525. Resolved by adding `TruckHead.len_m` (default 0.0, so existing files/fixtures and Task 1's `depth_m == 525` test are unchanged); the builder sets `y_m 525, len_m 35` (spec: y 525…560) → `depth_m == 560` for both small and demo. Task 3's `depth_m` definition in global constraints ("max y of blocks or truck heads") should be read as truck-head *end*.
2. Derived coordinates are rounded to 1 mm (`_r`) so the small layout has no float noise (e.g. STS-01 x0 66.667 instead of 66.66666666666667). Demo values are exact (75+150·n etc.).
3. Buffers order: offsets −8, +8, −16, +16, … (ids `BUF-<nn>-<k>`). Racks `RK-<B>-1..5` + reefer `RK-<B>-R`. Lanes `STS-nn-L1..L3`. Drive lanes `DL1..DL4`; every transfer zone's approach lane = `DL4` (landward-most).
4. STS `berth_ids` = the single berth containing x0 (demo: STS-01..04 → A, 05..08 → B).
5. Zones (names): `berth-<id>`, `agv-lanes` (whole apron y 40–165), `agv-parking` (y 92–98), per block `transfer-ws-<B>` (block x-range, y 110–160), `block-<B>`, `truck-interchange-<B>` (y 525–560), `maintenance-lane-<A>-<B>` (gaps between adjacent blocks, n−1). North strip (depth+20…+120 = y 580–680): `rail-interchange` (580–610) and `rail-terminal` (610–680) over the yard's x-range; gate complex `gate`/`ocr-gate`/`tech-gate`/`customs`/`problem-parking` side by side from yard east edge + 40 m to the quay end. East strip (same x, y 165…depth) split into `oog`, `building-tob`, `building-workshop`. Overlapping zones (e.g. parking inside agv_lanes) are allowed; validate doesn't check zone geometry.
6. Builder raises `ValueError` if blocks run past the quay end, if there is no room east of the yard for the gate/OOG strip, or for >26 berths/blocks; it calls `validate` before returning.

## Concerns
- The `depth_m` semantics change (truck-head end) is small but crosses into Task 1's model; reviewers should confirm it's the intended reading for Task 3 (`rows 28`).
- The 1200 m demo yard only spans x 25–684, so the east strip (x 724–1200) is wide; plausible but not to scale with any drawing.

---

## Fix round 1 (coordinator rulings) — commit `5fac114`

1. **The yard now spans the quay.** The demo has 24 blocks (A–X, x 25–1020). The east strip is now 20 m east of the yard (the gap constant was 40 m, now 20 m), so OOG and the buildings sit at x 1040–1200. The gate complex uses the same x range. The small layout stays at 4 blocks, x 25–180, with its strip at x 200–400. The >26-ids guard is kept. `gen_layouts.py` now builds the demo with `n_blocks=24`.
2. **Truck heads start at the block end.** Truck heads are at `y_m = block.y1_m` (425) with `len_m` 35, so `depth_m` is 460 for both layouts. The `TRUCK_HEAD_Y_M` constant is removed. `truck_interchange` zones cover y 425–460. The north strip follows the new depth: rail-interchange y 480–510, rail-terminal 510–580, and the gate complex 480–580. The docstrings are updated and record the spec's 165…525 slip.
3. **Minor review items:**
   - (a) The shipped-file test also asserts `yaml.safe_load(file) == to_yaml_dict(build())`.
   - (b) `_r` rounding now also applies to block `x0_m` and the last berth's `to_m`.
   - (c) New `test_small_matches_spec_values`: STS x0 66.667/200/333.333; buffer offsets −8, +8, −16, +16; block x0; berth.
4. **Updated demo tests:** 24 blocks, 144 racks, 48 unique ASC ids, last x1 1020, 23 maintenance lanes, 24 block/transfer/truck zones, and the oog, rail-interchange and rail-terminal coordinates.

Tests: the two shipped-file tests failed until the YAMLs were regenerated, then everything passed. The generator is still idempotent (md5 unchanged on re-run), and ruff is clean on the touched files. `tests/model` + architecture: 94 passed. Full suite: 670 passed, 17 deselected.

Task 3 impact: depth 460 gives rows = ceil(460/20) = 23, not 28, so Task 3's `test_grid_view_small` expectation (rows 28) must be updated to 23.
