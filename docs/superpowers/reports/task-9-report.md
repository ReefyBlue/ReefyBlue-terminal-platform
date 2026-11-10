# Task 9 report: World and SimEquipmentPort

Status: DONE_WITH_CONCERNS. Commit 3a866e1.

## Implemented
packages/sim/src/terminal_sim/world.py: `World` (env yieldless=False + seed, rng, grid, yard, 2 STS + 6 AGV + 4 ASC in that order, `port`, `run`, `now`, `resolve_cell`, `transfer_sts_to_agv`, `transfer_agv_to_tp`) and `SimEquipmentPort` (7 protocol methods, `_dispatch` synchronous in order, KeyError on unknown id). Port is created before equipment; all emit = port._dispatch.

## Tests (tests/sim/test_world.py, 5)
builds_from_baseline, assign_and_event_roundtrip, unknown_id_raises (snapshot/assign/cancel/reset), resolve_cell, transfer_sts_to_agv_and_agv_to_tp.

## TDD
RED: `ModuleNotFoundError: No module named 'terminal_sim.world'` (1 error during collection).
GREEN: `5 passed in 0.11s`; full suite `94 passed in 0.56s`.

## Concern
With the full baseline fleet (6 AGVs at (0..5,2)) AGV-01 cannot move: its only route start is (1,2), reserved by AGV-02 on one-way row 2, and AGV retries forever (BFS ignores reservations). So the brief's roundtrip (AGV-01 -> TP/A/WS in 300s) only works with n_agv=1. Tests use `dataclasses.replace(scenario, n_agv=1)` for the roundtrip and transfer tests. Dispatcher (later task) must pick AGVs that can leave (e.g. front-most), or start cells/routing need rework. Also ruff reports pre-existing errors elsewhere in packages/sim (not in new files if listed clean above).

## Self-review
Minimal, no extras. transfer_sts_to_agv raises KeyError for unknown STS id (not specified).

## Fix report: AGV deadlock (controller ruling)
Changed: `Grid.shortest_path(..., *, avoid=frozenset())` (BFS skips avoided cells except goal; start always allowed), new `Grid.reserved_by_others(owner)`. `AGV._plan(goal)` plans with avoid=reserved_by_others, falls back to unconstrained, else NO_ROUTE. Reserve-wait loop counts waiting; after `REPLAN_AFTER_S=30.0` on a non-goal cell it re-plans from the current cell (counter reset); goal cell keeps waiting. Per-step reservation discipline unchanged.
Tests: test_grid (avoid respected; reserved_by_others excludes own), test_agv (plans_around_parked_agv; replans_when_blocked_mid_route: waits <30s, then arrives at (8,2)), test_world (solo_world workaround removed; roundtrip and transfer use full n_agv=6 baseline).
Command: `uv run pytest tests/sim/test_grid.py tests/sim/test_agv.py tests/sim/test_world.py -q` -> `24 passed in 0.20s`; full suite `98 passed in 0.57s`.
