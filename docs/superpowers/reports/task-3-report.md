# Task 3 report: Scenario en layout laden

Status: DONE

## Implemented
- packages/sim/src/terminal_sim/scenario.py: FaultParams, Scenario, load_scenario (ValueError with field name; faults for every Kind, lower-case yaml keys, missing block -> zeros).
- packages/sim/src/terminal_sim/layout.py: Cell, StsSpec, BlockSpec, Layout, load_layout (reuses Position).
- terminal.yaml, scenarios/{baseline,agv4,faults}.yaml with brief values (ASC ids ASC-A..D match locations regex).

## TDD
- RED: `uv run pytest tests/sim -q` -> ModuleNotFoundError: No module named 'terminal_sim.scenario' (2 collection errors).
- GREEN: `uv run pytest tests/sim -q` -> 10 passed. Full suite: 41 passed.

## Tests
tests/sim/test_scenario.py (baseline, agv4/faults values, 7 rejection params incl. n_sts=3, n_containers=0, negative share), tests/sim/test_layout.py (geometry, lane cells, tp_ws cells, 960 slots, AGV starts).

## Self-review
Matches brief; no extra abstractions. Concern: none. Missing required yaml keys raise KeyError rather than ValueError (not in brief).
