# Task 6 report: Equipment base and STS

Status: DONE (with one salabim concern, below)

## Implemented
- packages/sim/src/terminal_sim/equipment/{__init__,base,sts}.py
- tests/sim/test_sts.py (13 tests)
- Equipment per brief and rulings. STS treats an AGV as in lane when either lane cell is owned by an id starting "AGV-"; `handover_lane_occupied` uses the same check. Cycle hold(triangular(90,150,110)), poll hold(5), energy +5.0.
- Extra (YAGNI-light) hook: `Equipment.accepts(job)` overridden by STS for the job-type filter.

## TDD
RED: test_sts.py failed at collection (ModuleNotFoundError terminal_sim.equipment). GREEN: 13 passed. Full suite run once, all passed.

## Salabim facts for AGV/ASC/World (important)
1. Salabim 26 defaults to `yieldless=True`, which imports `greenlet` (NOT installed) and crashes `Environment(...)`. Generator-style (`yield self.hold`) requires `salabim.Environment(trace=False, yieldless=False)`. World (Task 9) must do the same (or `salabim.yieldless(False)` globally).
2. Create components with `process=""` to get a passive data component (`process=None` auto-starts `process`). Start with `self.activate(process="process")`; works repeatedly after the previous run ends (component returns to "data").
3. `self.cancel()` stops a running process; verified a cancelled STS never completes later.
4. Component `name=equipment_id` is set via `super().__init__(name=..., env=..., process="")`.
5. `env.run(till=T)` with no pending events just advances to T.

## Self-review
No salabim warnings printed. Tests exercise real behaviour (timing bounds, late AGV, non-AGV owner, second lane cell, reuse after completion, cancel not completing later).

## Concerns
- Requires `yieldless=False` everywhere (see 1); alternatively add greenlet dependency.

## Fix report (review follow-up)
- base.py: `fail_job` now calls `self.cancel()` when invoked from outside the component's own process (`env.current_component() is not self`); `__init__` raises `ValueError("Equipment requires Environment(yieldless=False)")` when `env._yieldless`.
- sts.py: `process` returns without completing/adding energy if `current_job is None` after the hold.
- Tests: `test_fail_job_emits_and_idles` extended (run till 400, no JOB_COMPLETED, energy 0.0, IDLE); new `test_fail_job_mid_cycle_does_not_complete`; new `test_requires_non_yieldless_env` (stub env, since a yieldless Environment cannot be built without greenlet).
- Command: `uv run pytest tests/sim/test_sts.py -q` -> 15 passed.
