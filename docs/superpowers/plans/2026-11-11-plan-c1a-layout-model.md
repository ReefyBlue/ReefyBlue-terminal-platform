# Plan C1a — Layout Model in Metres Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A metre-based terminal layout model (`TerminalLayout`, YAML `version: 2`) with validation, a derived 20 m grid view so the existing sim/ECS/viewer keep working unchanged, two shipped layouts (`terminal_mv_small.yaml` for tests, `terminal_mv.yaml` demo), and `GET /layout` serving the full model.

**Architecture:** New module `terminal_model.layout` holds the dataclasses, loader, validator, a builder for standard terminals and `grid_view()` (the derived grid as plain data). `terminal_sim.layout.load_layout` detects `version: 2` and builds its legacy `Layout` from `grid_view()`; the ECS service does the same for `/layout` and adds a `terminal` object to the response. `terminal.yaml` stays the legacy v1 file; all existing fixtures stay byte-identical.

**Tech Stack:** Python 3.12, PyYAML, dataclasses (frozen), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-11-plan-c1-terminal-design.md` (§3, §3.1, §4; §1 success criteria 1, 4, 5)

## Global Constraints

- `terminal_model` imports no other `terminal_*` package (architecture test); `terminal_ecs_service` must not import `terminal_sim`.
- Legacy `terminal.yaml` (no `version` key) keeps loading exactly as today; all equivalence fixtures stay byte-identical; `uv run pytest -q`, `-m e2e_opcua tests/e2e -q` (skip `slow`) and `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -q` stay green.
- Units: metres everywhere in v2; origin = quay west corner, x along the quay, y landward; `bay_pitch_m 6.5`, `row_pitch_m 2.9`, `tier_h_m 2.9`; container 20' 6.058 m, 40' 12.192 m, width 2.438 m.
- Spec §3 demo values: quay 1200 m, berths A 0–600 / B 600–1200; STS rails y 10 / 40; 8 STS at x0 = 75 + 150·n, `min_gap_m 27`; apron y 40–165 with 3 lanes per STS (width 3.5, length 16, y 45–61), 4 drive lanes at y 70/76/82/88 alternating E/W, crossings every 50 m, 2 buffers per STS at y 95; transfer zone y 110–160 with 5 racks (pitch 3.5) + 1 reefer/exception rack per block; 16 blocks A–P, pitch 42 m, x0 = 25 + 42·i, y0 165, 40 bays × 10 rows × 5 tiers, `asc_min_gap_bays 2`, ASC ids `ASC-<B>-WS`/`ASC-<B>-LS`; truck heads y 525–560, 4 lanes. Small variant: quay 400 m, 3 STS, 4 blocks, 12 AGVs (buffers), same pitches.
- Derived grid: `cell_m 20`; `cols = ceil(quay_length_m / 20)`, `rows = ceil(depth_m / 20)` where `depth_m` = max y of blocks (y0 + bays·6.5) or truck heads; STS lane cells = the cells containing the STS's lane centres (deduplicated, ≥ 2, take the first two); `tp_ws_cell` = cell of rack 1 of the block's transfer zone; legacy `asc_id` = `asc_ws_id`; `agv_start_cells` = buffer cells in STS order.
- Commit trailer lines: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_011Q6oL3tzJGQycNuMVuzTtk`.
- Branch `feat/plan-c1-terminal-design` (base `feat/plan-b2c-deploy-soak`).

## Review Focus

1. A v2 YAML with two blocks whose x-ranges overlap (pitch < 29 m) — must fail validation with a message naming both blocks, not load (Task 1 `test_overlapping_blocks_rejected`).
2. A v2 YAML without `zones:` or without `landside:` — must load (optional sections default empty) (Task 1 `test_optional_sections_default_empty`).
3. A derived grid where two STS lanes fall in the same 20 m cell — lane cells must still be two distinct cells (Task 3 `test_grid_view_lane_cells_distinct`).
4. The ECS service started with a v2 layout while the viewer (unchanged) reads the legacy fields of `/layout` — `worldW/worldH` and `sts[].x` must still be present and consistent with `grid` (Task 4 `test_layout_v2_keeps_legacy_fields`).
5. `load_layout` on a v2 file whose derived `rows` exceeds what the sim's `Grid` supports (none today, but depth 560 m → 28 rows) — the in-process baseline must still complete on `terminal_mv_small.yaml` (Task 3 fixture run).

---

## File structure

- Create `packages/model/src/terminal_model/layout.py` (dataclasses + `load_terminal` + `validate` + `LayoutError`), `packages/model/src/terminal_model/layout_grid.py` (`grid_view`), `packages/model/src/terminal_model/layout_builder.py` (`standard_terminal(...)` + `to_yaml_dict`).
- Create `terminal_mv_small.yaml`, `terminal_mv.yaml` (repo root, next to `terminal.yaml`), `scripts/gen_layouts.py` (writes both from the builder; tests assert the files equal the builder output).
- Modify `packages/sim/src/terminal_sim/layout.py` (v2 branch), `packages/ecs_service/src/terminal_ecs_service/{serve.py,app.py,schemas.py}` (v2 loader + `terminal` in `/layout`), `README.md` (section "Layout in meters (C1a)").
- Tests: `tests/model/test_layout_v2.py`, `tests/model/test_layout_grid.py`, `tests/model/test_layout_builder.py`, `tests/sim/test_layout_v2_sim.py`, `tests/ecs_service/test_layout_v2.py`, `tests/e2e/fixtures/mv_small_baseline_seed1.json`, `tests/e2e/test_mv_small_baseline.py`.

---

### Task 1: `terminal_model.layout` — dataclasses, loader, validation

**Files:**
- Create: `packages/model/src/terminal_model/layout.py`
- Test: `tests/model/test_layout_v2.py`

**Interfaces (Produces):**
```python
@dataclass(frozen=True) class Berth: id: str; from_m: float; to_m: float
@dataclass(frozen=True) class Quay: length_m: float; berths: tuple[Berth, ...]
@dataclass(frozen=True) class Rails: y_ws_m: float; y_ls_m: float
@dataclass(frozen=True) class Lane: id: str; x_m: float; y_m: float; len_m: float; width_m: float
@dataclass(frozen=True) class StsSpec: id: str; x0_m: float; min_gap_m: float; berth_ids: tuple[str, ...]; lanes: tuple[Lane, ...]
@dataclass(frozen=True) class DriveLane: id: str; y_m: float; direction: Literal["E", "W"]
@dataclass(frozen=True) class Buffer: id: str; x_m: float; y_m: float; sts_id: str
@dataclass(frozen=True) class Apron: y_from_m: float; y_to_m: float; drive_lanes: tuple[DriveLane, ...]; crossings_every_m: float; buffers: tuple[Buffer, ...]
@dataclass(frozen=True) class Rack: id: str; x_m: float; y_m: float; size: Literal["20", "40", "45"]; reefer: bool = False
@dataclass(frozen=True) class TransferZone: block: str; racks: tuple[Rack, ...]; approach_lane_id: str
@dataclass(frozen=True) class Block: id: str; x0_m: float; y0_m: float; bays: int; rows: int; tiers: int; asc_ws_id: str; asc_ls_id: str; bay_pitch_m: float = 6.5; row_pitch_m: float = 2.9; tier_h_m: float = 2.9; asc_min_gap_bays: int = 2; maintenance_lane_side: Literal["E", "W"] = "E"; reefer_rows: tuple[int, ...] = ()
    # properties: width_m = rows*row_pitch_m; length_m = bays*bay_pitch_m; x1_m, y1_m
@dataclass(frozen=True) class TruckHead: block: str; lanes: int; y_m: float
@dataclass(frozen=True) class Zone: name: str; type: str; polygon: tuple[tuple[float, float], ...]   # type in ZONE_TYPES (spec §3.1 table)
@dataclass(frozen=True) class OpcUa: base_port: int = 48400; host: str = "localhost"
@dataclass(frozen=True) class TerminalLayout: name: str; quay: Quay; sts_rails: Rails; sts: tuple[StsSpec, ...]; apron: Apron; transfer_zones: tuple[TransferZone, ...]; blocks: tuple[Block, ...]; landside: tuple[TruckHead, ...]; zones: tuple[Zone, ...]; opcua: OpcUa
    # helpers: block(id) -> Block; transfer_zone(block_id) -> TransferZone; depth_m -> float
class LayoutError(ValueError): ...
def is_v2(raw: dict) -> bool            # raw.get("version") == 2
def load_terminal(path: Path) -> TerminalLayout   # yaml → dataclasses → validate()
def validate(t: TerminalLayout) -> None # raises LayoutError listing every violation (one line each)
ZONE_TYPES = frozenset({"berth","agv_lanes","agv_parking","transfer_ws","block","truck_interchange","rail_interchange","rail_terminal","gate","ocr_gate","tech_gate","customs","problem_parking","oog","building","maintenance_lane"})
```
Validation rules (spec §4): no overlapping block rectangles; every block has a transfer zone and vice versa; racks inside their zone's y-range (`apron.y_to_m ≤ rack.y_m < block.y0_m`) and within the block's x-range ± 10 m; STS rails inside `[0, apron.y_from_m]`; every STS has ≥ 1 lane and `min_gap_m ≥ 27`; `asc_min_gap_bays ≥ 1`; berths cover `[0, quay.length_m]` without gaps/overlaps; STS `berth_ids` exist; unique ids for STS, lanes, racks, blocks, ASCs, buffers, zones; zone types in `ZONE_TYPES`; `reefer_rows` within `0..rows-1`.

- [ ] **Step 1: Write the failing tests** — a module-level `MINI_V2` YAML string (quay 200 m, 1 berth, 1 STS with 2 lanes, apron with 1 drive lane + 1 buffer, 1 block `A` with a zone of 2 racks, 1 truck head, 1 zone of type `oog`). Tests: `test_load_mini_v2_roundtrip` (field values), `test_block_properties` (`width_m == 29.0`, `length_m == 260.0`), `test_overlapping_blocks_rejected` (Review Focus 1; message contains both ids), `test_missing_transfer_zone_rejected`, `test_rack_outside_zone_rejected`, `test_unknown_zone_type_rejected`, `test_duplicate_ids_rejected`, `test_optional_sections_default_empty` (Review Focus 2), `test_is_v2_false_for_legacy` (`terminal.yaml`).
- [ ] **Step 2: Run** `uv run pytest tests/model/test_layout_v2.py -v` → FAIL (import).
- [ ] **Step 3: Implement** per Interfaces; collect all violations before raising.
- [ ] **Step 4: Run** → PASS; `uv run pytest tests/test_architecture.py -q` → PASS.
- [ ] **Step 5: Commit** — `feat(model): metre-based TerminalLayout v2 with validation`.

---

### Task 2: Builder and the two shipped layouts

**Files:**
- Create: `packages/model/src/terminal_model/layout_builder.py`, `scripts/gen_layouts.py`, `terminal_mv_small.yaml`, `terminal_mv.yaml`
- Test: `tests/model/test_layout_builder.py`

**Interfaces (Produces):**
```python
def standard_terminal(*, name: str, quay_length_m: float, n_berths: int, n_sts: int, n_blocks: int, n_agv_buffers_per_sts: int = 2, racks_per_block: int = 5, block_bays: int = 40, block_rows: int = 10, block_tiers: int = 5, block_pitch_m: float = 42.0, first_block_x_m: float = 25.0, opcua_base_port: int = 48400) -> TerminalLayout
def to_yaml_dict(t: TerminalLayout) -> dict      # version: 2, plain types, deterministic key order
```
`standard_terminal` applies every Global-Constraints value (STS x0 = 75 + 150·n for the 1200 m demo generalises to evenly spaced `quay_length_m/n_sts` centres; lanes at x0−3.5/x0/x0+3.5; drive lanes y 70/76/82/88; crossings 50; buffers at y 95, x0±8; racks at x = block centre −7 … +7 step 3.5, y 135, sizes "40", plus one `reefer: True` rack at x0+14; blocks x0 = first_block_x_m + pitch·i, y0 165; `reefer_rows = (rows-1,)`; truck heads y 525 with 4 lanes; zones: `berth` (0..quay × 0..40), `agv_lanes` (apron), `agv_parking` (buffers strip y 92–98), `transfer_ws` per block, `block` per block, `truck_interchange` per block, `maintenance_lane` between blocks, and the §3.1 placeholders `rail_interchange`+`rail_terminal` (north strip y depth+20..depth+120), `gate`/`ocr_gate`/`tech_gate`/`customs`/`problem_parking` (north-east), `oog` and `building` ×2 (east strip)). Small variant = `standard_terminal(name="mv_small", quay_length_m=400, n_berths=1, n_sts=3, n_blocks=4, n_agv_buffers_per_sts=4)` (12 buffers → 12 AGV start cells); demo = `standard_terminal(name="mv", quay_length_m=1200, n_berths=2, n_sts=8, n_blocks=16)`.

- [ ] **Step 1: Failing tests** — `test_small_counts` (3 STS, 4 blocks, 4 zones of racks ×6, 12 buffers, validates), `test_demo_counts` (8 STS, 16 blocks, 96 racks, depth 560), `test_yaml_roundtrip` (`load_terminal(tmp yaml of to_yaml_dict(t)) == t`), `test_shipped_files_match_builder` (both repo files equal `to_yaml_dict` of the builder calls above; the test is the guard against hand edits drifting from the generator).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** builder + script (`uv run python scripts/gen_layouts.py` writes both files). **Step 4: Run** → PASS. **Step 5: Commit** — `feat(model): standard terminal builder; terminal_mv_small.yaml and terminal_mv.yaml`.

---

### Task 3: Derived grid view and the sim on a v2 layout

**Files:**
- Create: `packages/model/src/terminal_model/layout_grid.py`
- Modify: `packages/sim/src/terminal_sim/layout.py` (`load_layout`: `is_v2` → `grid_view` → legacy `Layout`)
- Test: `tests/model/test_layout_grid.py`, `tests/sim/test_layout_v2_sim.py`, `tests/e2e/test_mv_small_baseline.py` + fixture `tests/e2e/fixtures/mv_small_baseline_seed1.json`

**Interfaces (Produces):**
```python
@dataclass(frozen=True) class GridView: cell_m: float; cols: int; rows: int; sts: tuple[GridSts, ...]; blocks: tuple[GridBlock, ...]; agv_start_cells: tuple[Cell, ...]
@dataclass(frozen=True) class GridSts: id: str; x_m: float; y_m: float; lane_cells: tuple[Cell, Cell]
@dataclass(frozen=True) class GridBlock: id: str; asc_id: str; tp_ws_m: tuple[float, float]; tp_ws_cell: Cell; bays: int; rows: int; tiers: int
def grid_view(t: TerminalLayout, cell_m: float = 20.0) -> GridView
def cell_of(x_m: float, y_m: float, cell_m: float) -> Cell
```
`terminal_sim.layout.load_layout(path)`: unchanged for v1; for v2 returns `Layout(quay_length_m, grid_cell_m, cols, rows, sts=[StsSpec(...)], blocks=[BlockSpec(block=id, asc_id, tp_ws=Position(*tp_ws_m), tp_ws_cell, bays, rows, tiers)], agv_start_cells, opcua_*)`. Lane cells: if two lanes map to the same cell, use that cell and its east neighbour (Review Focus 3). `tp_ws_cell` must not coincide with any STS lane cell or another block's cell (validate in `grid_view`, raise `LayoutError`).

- [ ] **Step 1: Failing tests** — `test_grid_view_small` (cols 20, rows 28, 3 STS with 2 distinct lane cells each, 4 blocks with distinct tp cells, 12 start cells), `test_grid_view_lane_cells_distinct` (Review Focus 3: two lanes 3.5 m apart in one cell), `test_sim_load_layout_v2_matches_grid_view` (legacy `Layout` fields equal the view), `test_sim_load_layout_v1_unchanged` (`terminal.yaml` identical to before — compare to a literal of today's values).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.**
- [ ] **Step 4: Baseline on the small layout** — `tests/e2e/test_mv_small_baseline.py` (marker `e2e`): `run_once(scenario=scenarios/baseline.yaml with n_sts=3, n_agv=12, layout=terminal_mv_small.yaml, seed=1)` → first run writes `tests/e2e/fixtures/mv_small_baseline_seed1.json` (same shape as `plan_a_baseline_seed1.json`), later runs compare exactly; assert `done == n_containers`, `failed == 0`. Run twice; must be identical.
- [ ] **Step 5: Run** full default suite + `uv run pytest tests/e2e/test_baseline.py tests/e2e/test_service_equivalence.py tests/e2e/test_faults_equivalence.py -q` (legacy fixtures exact).
- [ ] **Step 6: Commit** — `feat(sim): v2 layouts load through a derived 20 m grid; mv_small baseline fixture`.

---

### Task 4: ECS service — v2 `/layout`

**Files:**
- Modify: `packages/ecs_service/src/terminal_ecs_service/serve.py` (`load_layout`: v2 → `grid_view` → existing service `Layout` dataclass + attach `terminal`), `app.py` (`get_layout` adds `terminal` and `zones`), `schemas.py` (`TerminalOut` mirrors `TerminalLayout` via pydantic models; `LayoutOut.terminal: TerminalOut | None`, `LayoutOut.version: int` (1 or 2))
- Test: `tests/ecs_service/test_layout_v2.py`

**Interfaces (Produces):** `GET /layout` on a v2 layout → all legacy fields (from `grid_view`) + `version: 2` + `terminal: {name, quay, sts_rails, sts[], apron, transfer_zones[], blocks[], landside[], zones[], opcua}`; on v1 → `version: 1`, `terminal: null`.

- [ ] **Step 1: Failing tests** — `test_layout_v2_keeps_legacy_fields` (Review Focus 4: `quay_length_m == 400`, `grid_cols == 20`, `sts[0]["x"]` equals the grid view, `blocks[0]["tp_ws"]` present), `test_layout_v2_has_terminal` (`terminal.blocks` 4, `terminal.transfer_zones[0].racks` 6, `zones` non-empty with types in `ZONE_TYPES`), `test_layout_v1_has_no_terminal`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (service may import `terminal_model.layout`, `layout_grid`; not `terminal_sim`).
- [ ] **Step 4: Run** `uv run pytest tests/ecs_service tests/test_architecture.py -q` → PASS; `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -q` → PASS (viewer unchanged, legacy fields suffice).
- [ ] **Step 5: Commit** — `feat(ecs_service): /layout serves the v2 terminal model alongside the legacy grid fields`.

---

### Task 5: End-to-end on the small v2 layout + docs

**Files:**
- Modify: `tests/e2e/opcua_procs.py` (`sim_and_gateway(..., layout=ROOT/"terminal.yaml")` parameter, default unchanged; `ecs_service(..., layout=...)`), `README.md` (section "Layout in meters (C1a)": v2 format, the two files, `scripts/gen_layouts.py`, how the grid is derived, what C1b/C3 will add)
- Test: `tests/e2e/test_mv_small_opcua.py` (marker `e2e_opcua`)

- [ ] **Step 1: Write the test** — start sim (`--layout terminal_mv_small.yaml`, scenario baseline with `n_sts 3, n_agv 12`), gateway (`--n-sts 3 --n-agv 6`? — check how the gateway derives the fleet: it must read the v2 file too; if it reads `blocks` for ASC count, it goes through `terminal_sim.layout.load_layout`, which Task 3 made v2-aware), ECS service; `run_once_http` the baseline; assert `done == n_containers`; `GET /layout["version"] == 2`.
- [ ] **Step 2: Run** `uv run pytest -m e2e_opcua tests/e2e/test_mv_small_opcua.py -q` → PASS. Fix the gateway's `--n-sts/--n-agv` derivation if it assumes v1.
- [ ] **Step 3: README section; run** `uv run pytest -q` → PASS.
- [ ] **Step 4: Commit** — `test(e2e): OPC UA stack on terminal_mv_small.yaml; README layout section`.
