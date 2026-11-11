# Plan C1a — final-review fix report

Branch `feat/plan-c1-terminal-design`, commit `d117acc`. Paths are relative to the repo root.

## Important 1: rack/lane overlap validation
- `packages/model/src/terminal_model/layout.py:25-32` adds the constants `RACK_HALF_WIDTH_M` (1.219) and `RACK_CLEARANCE_M` (0.2), plus a comment saying racks are oriented along y.
- `:315` adds `_overlaps` (a 1-D interval check; touching is allowed within an epsilon of 1e-6).
- `:383` checks the lanes of each STS on x ± width/2. The message is `lanes 'X' and 'Y' overlap`.
- `:432-433` checks racks per transfer zone on x ± (1.219 + 0.2). The message is `racks 'X' and 'Y' overlap`.
- **Deviation from the findings:** the findings asked for half-*length* by size (20'→3.029, 40'→6.096, 45'→6.858 m). The orchestrator asked for half-*width* instead. Racks stand side by side across the block width and hold the box along y, so their x-extent is the container width. With half-length, the shipped racks at a 3.5 m pitch would all overlap.
- Shipped geometry: racks at 3.5 m pitch with an extent of 2.838 m do not overlap. Lanes 3.5 m wide at 3.5 m pitch touch, which is allowed.
- Tests in `tests/model/test_layout_v2.py`:
  - `test_overlapping_racks_rejected` (:181)
  - `test_overlapping_lanes_rejected` (:190)
  - `test_touching_lanes_allowed` (:197)
  - `test_shipped_layouts_validate[terminal_mv_small.yaml|terminal_mv.yaml]` (:260)

## Important 2: fixture never regenerated silently
- `tests/e2e/test_mv_small_baseline.py:34-37` writes the fixture only when `WRITE_FIXTURES=1`. Otherwise it fails with `missing fixture …; regenerate deliberately with WRITE_FIXTURES=1`.
- The legacy fixture tests (`test_plan_a_equivalence`, `test_faults_equivalence`, `test_service_equivalence`) only read their fixtures. `test_baseline.py` uses no fixture. They needed no change.
- Covered by the test itself (it passes against the committed fixture).

## Important 3: duplicate berth and drive-lane ids
- `layout.py:327-328` adds `_dupes("berth", …)` and `_dupes("drive lane", …)`.
- Tests: `test_duplicate_berth_ids_rejected` (:202) and `test_duplicate_drive_lane_ids_rejected` (:210).

## Minor 1: comment states the rule
- `layout.py:424-425` now reads: a rack lies between `apron.y_from_m` and the block's `y0_m`; the apron may extend to the block head.

## Minor 2: OpcUa coercion and strict reefer bool
- `layout.py:283` adds `_opcua`, which applies `int(base_port)` and `str(host)`.
- `layout.py:39` adds `_strict_bool`, which accepts bool or "true"/"false" in any case and raises `LayoutError` for anything else. It is used at `:254`.
- `load_terminal` prefixes the path onto a `LayoutError` raised during build.
- Tests:
  - `test_opcua_values_coerced` (:217)
  - `test_reefer_strict_bool_accepts` (:224, 5 cases)
  - `test_reefer_non_bool_rejected` (:231; cases "yes", "no", 1, 0, "1", None)

## Minor 3: TerminalOut drift guard
- `packages/ecs_service/src/terminal_ecs_service/schemas.py:119` adds a `_Strict` base with `extra="forbid"`. TerminalOut and all 14 of its sub-models now derive from it.
- Test `tests/ecs_service/test_layout_v2_endpoint.py:60` `test_terminal_out_mirrors_dataclasses` checks three things:
  - For all 15 pairs, the dataclass field names equal `model_fields`.
  - Every model has `extra == "forbid"`.
  - An unknown key raises a `ValidationError`.

## Minor 4: positive-dimension checks
- `layout.py:337` and following check that quay `length_m` > 0, block bays/rows/tiers > 0, lane `len_m`/`width_m` > 0, and every zone polygon has at least 3 points.
- Tests: `test_non_positive_dimensions_rejected` (:237) and `test_non_positive_quay_length_rejected` (:251).

## Minor 5: spec §1 criterion 1
- `docs/superpowers/specs/2026-11-11-plan-c1-terminal-design.md:12` now reads "`terminal_mv*.yaml` beschrijven de terminal volledig in meters (`terminal.yaml` blijft de legacy v1-layout)…".

## Minor 6: exact block-overlap message
- `test_overlapping_blocks_rejected` asserts `blocks 'A' and 'B' overlap`.

## Minor 7: gateway logs the control port
- `packages/gateway/src/terminal_gateway/gateway.py:110` adds `log.info("gateway control port %d", self.control_port)`.
- No dedicated test; gateway start-up is exercised by `tests/gateway`.

## Verification
```
$ uv run pytest tests/model tests/sim tests/gateway tests/ecs_service tests/e2e/test_mv_small_baseline.py tests/e2e/test_baseline.py -q
343 passed in 178.61s
$ uv run pytest -q
714 passed, 18 deselected in 273.33s
```

## Notes
- The commit trailer follows the session's attribution reminder (`Claude Opus 5.5`). The findings file listed a different Co-Authored-By name.
- The rack overlap check uses x only. Racks in one zone at different y with overlapping x would be flagged as overlapping. The shipped layouts have none. If staggered racks are ever needed, this check should become a 2-D check.
