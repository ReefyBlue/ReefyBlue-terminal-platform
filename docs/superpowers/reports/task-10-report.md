# Task 10 report: Storingen (FaultInjector)

Status: DONE. Commit 3d18c32 `feat(sim): fault injection with MTBF/MTTR` on feat/core-plan-a.

Implemented: `packages/sim/src/terminal_sim/faults.py` (FaultInjector, count, process loop per brief, injected rng only);
`world.py`: `fault_injectors` list (created when `faults[kind].mtbf_s > 0`) and `faults_total()`.

Tests (tests/sim/test_faults.py): test_fault_cycle (AGV mtbf 100/mttr 10, run 1000: count>=5, AGV-01 states start FAULT and alternate FAULT/IDLE),
test_fault_fails_running_job (JOB_FAILED reason FAULT, job J1), test_no_injector_when_mtbf_zero.

RED: 3 failed (AttributeError: 'World' object has no attribute 'fault_injectors'; assert []).
GREEN: 3 passed in 0.09s. Full suite: 101 passed.

Self-review: minimal, no extras beyond faults_total. Note: injector skips the cycle if the target is already FAULT.
Concerns: repo-wide ruff reports 16 pre-existing errors elsewhere (not touched).

## Fix report (review findings)
1. Injector now sets FAULT first, then `fail_job("FAULT")`; `Equipment.fail_job` only goes IDLE when state is not FAULT. Sequence: STATE_CHANGED FAULT, JOB_FAILED, later IDLE. Covered by extended `test_fault_fails_running_job` (no IDLE between FAULT and JOB_FAILED; AGV is FAULT after).
2. `load_scenario` validates mtbf_s>=0, mttr_s>=0, mttr_s>0 when mtbf_s>0 (ValueError naming e.g. "faults.agv.mttr_s"); `FaultInjector.__init__` raises ValueError on mtbf_s<=0 or mttr_s<=0. Covered by `test_scenario_rejects_bad_faults` (parametrised) and `test_injector_rejects_non_positive_params`.
3. `test_fault_cycle` now asserts `faults_total() >= 5`.
Command: `uv run pytest tests/sim/test_faults.py tests/sim/test_sts.py tests/sim/test_scenario.py -q` -> 31 passed. Full suite: 105 passed.
