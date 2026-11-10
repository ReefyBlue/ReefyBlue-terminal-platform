# Task 7 report: AGV

Status: DONE. Commit 41e8252 "feat(sim): AGV routing, reservations, battery and energy".

## Implemented
packages/sim/src/terminal_sim/equipment/agv.py: AGV(Equipment) per brief and decisions. Start cell reserved
(ValueError START_CELL_OCCUPIED), TRANSPORT only, speed 6/4 m/s, load/unload, route walk with reserve-just-before-move and
release-after-arrival, battery/energy accounting, NO_ROUTE failure, `_pending` cleanup in cancel_job and fail_job, extra().

## Tests (tests/sim/test_agv.py, 7)
speed/load, start-cell reservation, non-transport rejected, arrives and holds cell, two AGVs never share cell
(env.step loop, bounded), cancel mid-route leaves exactly one owned cell, unreachable goal -> NO_ROUTE.

## TDD
RED: `ModuleNotFoundError: No module named 'terminal_sim.equipment.agv'` (collection error).
GREEN: `7 passed in 0.10s`. Full suite: `83 passed in 0.46s`.

## Self-review
No extra features. Tests assert real behaviour. Lint clean on the new files after import-sort fix commit (repo-wide ruff has 18 pre-existing errors elsewhere).

## Concerns
None blocking. A stationary AGV keeps its cell reserved, so another AGV's route through it waits indefinitely (by design; no deadlock avoidance).
