# Task 8 report: ASC

Status: DONE.

Implemented `packages/sim/src/terminal_sim/equipment/asc.py` (ASC, constants per brief) and `tests/sim/test_asc.py` (6 tests: the 4 from the brief plus STACK_OUT success and wrong-block/bad-location rejection).

Cycle model: leg(current -> pick) + hoist(pick) + leg(pick -> place) + hoist(place); TP = bay 0,row 1,tier 1.
Computed time for STACK_IN A/10/3/1 from gantry bay 1: 1.625 + 6 + 20.25 + 6 = 33.875 s (not the brief's 52.5 s; test computes it from the constants, tolerance 1 s).

TDD: RED = `ModuleNotFoundError: No module named 'terminal_sim.equipment.asc'` (collection error). GREEN = `6 passed`. Full suite: 89 passed.
Ruff fixed (unused import/ordering) and formatted.

Self-review / concerns:
- After a job the trolley row isn't tracked; next leg assumes row 1 (gantry_bay only, per spec). Minor inaccuracy.
- STACK_OUT leaves gantry_bay 0.0 as specified.
- tp_ws_occupied/tp_ls_occupied are static flags, nothing sets them yet.
- Out-of-range slot on STACK_IN fails with "SLOT_OUT_OF_RANGE"; out-of-range STACK_OUT fails SLOT_EMPTY.
