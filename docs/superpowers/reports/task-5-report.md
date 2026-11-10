# Task 5 report: Grid routing and reservations

Status: DONE

## Implemented
`packages/sim/src/terminal_sim/grid.py`: `Cell` alias (exported), `Grid` with neighbors (one-way rows, bounds-checked, order: horizontal, y-1, y+1), BFS `shortest_path`, `try_reserve`/`release`/`owner_of`, `center`. Out-of-grid cells raise ValueError in all but owner_of.

## Tests (`tests/sim/test_grid.py`, 8 tests)
one-way lanes, edge neighbours, path direction, path length/exact path, unreachable/trivial, reserve conflict, center (2,5)->(50,110), out-of-grid ValueError.

## TDD
RED: ModuleNotFoundError terminal_sim.grid on collection.
GREEN: after implementing, 1 failure was a wrong expectation in my test (path (5,0)->(3,0) is 5 cells, not 6); fixed test. Full suite: all pass (61).

## Self-review
No YAGNI extras. Tests exercise real behaviour. Note: layout.py also defines its own `Cell`; grid.py defines its own identical alias per instructions (could later be unified).
