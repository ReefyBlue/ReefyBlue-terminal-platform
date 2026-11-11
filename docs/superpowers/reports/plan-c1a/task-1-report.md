# Task 1 report: terminal_model.layout

- Added `packages/model/src/terminal_model/layout.py` (dataclasses, `load_terminal`, `is_v2`, `validate`, `LayoutError`, `ZONE_TYPES`), tests in `tests/model/test_layout_v2.py` (12 tests), `pyyaml>=6` added to terminal-model deps (uv.lock updated).
- RED: collection failed with `No module named 'terminal_model.layout'`. GREEN: 12/12 pass; test_architecture passes; full `uv run pytest -q`: 660 passed, 17 deselected.
- Validation collects all violations, one line each; malformed YAML (missing keys) raises LayoutError.
- DEVIATION: brief says racks need `apron.y_to_m <= rack.y_m < block.y0_m`, but spec demo has apron y_to 165 = block y0 and racks at y 110-160, which that rule would reject. Implemented `apron.y_from_m <= rack.y_m < block.y0_m`. Needs decision from plan author.
- Extra checks beyond brief: unknown approach_lane_id, buffer sts_id, truck-head block, transfer zone for unknown block, duplicate transfer zone.
- Not run: e2e_opcua / e2e_ui suites (module is additive and unused).
