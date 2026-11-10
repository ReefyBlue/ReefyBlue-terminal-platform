# Task 15 report: Runner CLI

**Status: DONE_WITH_CONCERNS.** The runner works and the 20-container run completes. I made 4 integration fixes. Scenarios with **3 or more AGVs still gridlock** because of a parking gap in the design (see Concerns). Every shipped scenario (`baseline`, `agv4`, `faults`) hits its 12 h cap with 196–198 of 200 containers done.

Branch `feat/core-plan-a`. Commits, oldest first:

| commit | message |
|---|---|
| 360b929 | fix(sim): run a job assigned from the equipment's own completion event |
| d7236d2 | fix(sim): re-resolve a blocked AGV's goal so it can take the other lane cell |
| d365349 | fix(sim): put a discharged box on the lane AGV that was sent for it |
| ff09c0f | fix(ecs): issue a loaded AGV's next leg only once it is available |
| d121bb3 | fix(sim): break head-on AGV deadlocks by giving way |
| 0afa57a | feat(sim): scenario runner with replications and summary |

## Implemented (`packages/sim/src/terminal_sim/run.py`)
- `run_once(scenario, layout, seed)` follows the brief exactly:
  - It builds `replace(scenario, seed=seed)`, then World, `SimpleSlotAllocator`, and `generate_workorders(rng=random.Random(seed))`.
  - It creates the Dispatcher with the default lookahead and sets the `on_transfer` hook as specified. Hook errors raise `RuntimeError`, and an unknown kind raises `ValueError`.
  - It creates `KpiCollector(faults_total=world.faults_total)`, calls `prefill`, `submit` and `tick`, then loops: `t += 60; world.run(t); tick(); sample()`.
  - The loop stops when `pending == 0 and active_jobs == 0`, or at `duration_s`.
  - It returns `kpi.finish(world.now())`, with `sim_elapsed_s` and `timed_out` appended at the end.
- `run_replications(scenario_path, replications, out_dir, layout_path="terminal.yaml")`:
  - Runs seeds `seed + i`.
  - Deletes any stale CSV, then writes rows with `to_csv_row`.
  - Writes `<name>-summary.json` with `{kpi: {mean, p5, p95}}`. The percentiles use nearest rank.
  - Logs one JSON line per run on `terminal_sim.run` (run_id, seed, containers_done, moves_per_hour_total, timed_out).
  - Returns the summary path.
- `main(argv)` uses argparse with `scenario`, `--replications`, `--out`, `--layout`. It calls `configure_json_logging()`, prints the summary path and returns 0. The `__main__` guard is in place. I smoke-tested it with `python -m terminal_sim.run scenarios/agv4.yaml --replications 2 --out /tmp/...`.
- `KpiCollector.to_csv_row` is now a `@staticmethod`. It never used `self`, and the runner has no collector left after `run_once`. Existing instance calls still work.

## Tests (`tests/sim/test_run.py`)
The small scenario uses 20 containers, 1 STS, 2 AGVs, `duration_s: 7200`, no faults, and `discharge_share: 0.5`. Half load orders mean all four transfer hooks are exercised.

- `test_run_once_small` checks:
  - `containers_done == 20`, `containers_failed == 0`, `moves_per_hour_total > 0` and `timed_out == 0`
  - `0 < sim_elapsed_s < duration_s`
  - the last two keys are `sim_elapsed_s` and `timed_out`
- `test_reproducible`: two runs with the same seed give identical dicts.
- `test_cli_writes_csv_and_summary` checks:
  - a stale CSV is replaced, and the new one has a header plus 2 rows
  - stdout is the summary path
  - the JSON summary has `p5 <= mean <= p95` and `mean > 0`
  - the stderr JSON log lines carry run_id/seed/timed_out for seeds 7 and 8
  - a fixture restores the root logging handlers afterwards

Integration fixes:

| Test | Package | Fix it covers |
|---|---|---|
| `test_job_assigned_from_completion_event_runs` | sim, `test_sts.py` | 1 |
| `test_blocked_goal_is_re_resolved` | sim, `test_agv.py` | 2 |
| `test_transfer_sts_to_agv_picks_the_agv_sent_for_the_container` | sim, `test_world.py` | 3 |
| `test_loaded_agv_gets_next_leg_once_idle_without_rejections` | ecs, `test_dispatcher_load.py` | 4 |
| `test_head_on_deadlock_is_broken_by_giving_way` | sim, `test_agv.py` | give-way (commit d121bb3) |
| `test_waiting_registry` | sim, `test_grid.py` | give-way (commit d121bb3) |

### TDD
- RED, runner: `uv run pytest tests/sim/test_run.py -q` failed with `E ModuleNotFoundError: No module named 'terminal_sim.run'` / `1 error in 0.08s`.
- After the first implementation, all 3 tests failed: `AttributeError: 'NoneType' object has no attribute 'send'` (salabim). That was integration fix 1.
- RED for each regression test, each run before its fix:
  - fix 1: the same `AttributeError ... 'send'`
  - fix 2: `1 failed, 9 passed`
  - fix 3: `assert 'AGV-01' == 'AGV-02'`
  - fix 4: `assert ['rejected C9-TRANSPORT-2 by AGV-01'] == []`
  - give-way: `1 failed, 10 passed`
- GREEN: `tests/sim/test_run.py`: `3 passed in 0.58s`. Full suite, run once: `172 passed in 12.07s`. The slowest test is the pre-existing `test_allocator` at 9.96 s. The runner tests take about 0.2 s each.

## Integration fixes
1. **sim / `equipment/base.py`: equipment could not take a job assigned from inside its own process.**
   - Cause: `complete_job` emits JOB_COMPLETED and then STATE_CHANGED→IDLE. The dispatcher reacts synchronously and assigns the next job to the same crane or AGV while its process is still running. `activate(process=...)` on the current component swapped its generator. The old one then returned and salabim terminated the component, which crashed on `send`.
   - Fix: `assign_job` only activates from outside. A `_run` driver loop runs `process()` once per job and picks up a job that was assigned from within. A job that is cancelled or faulted from outside still stops `_run`, as before.
2. **sim / `equipment/agv.py`: an AGV never re-resolved a goal that another AGV held.**
   - Cause: `World.resolve_cell("STS-xx")` picks the first lane cell no AGV holds *at plan time*. Two AGVs dispatched together both chose cell 1. The loser waited next to it forever, because there was no replan when the blocked cell is the goal. So only one lane cell was ever used.
   - Fix: after `REPLAN_AFTER_S`, a blocked AGV re-resolves `to_loc` and replans if the goal changed.
3. **sim / `world.py`: a discharged box went onto the wrong lane AGV.**
   - Cause: `transfer_sts_to_agv` took the first empty AGV in the lane, in equipment-id order. With the lookahead AGV also in the lane, C0003 went onto AGV-01, which had been sent for C0004. The hook's `agv_to_tp` on AGV-02 then found it empty, and the next discharge found no empty AGV: two orders failed with TRANSFER_ERROR and two AGVs were quarantined.
   - The hook contract only passes the STS id, so I fixed it in the sim instead of ECS. `AGV.job_container_id` records the container of the last accepted job, and the transfer only picks a lane AGV that was sent for that container.
   - This is stricter: an AGV that was not sent for the box returns None, which surfaces as a real mismatch.
4. **ecs / `dispatcher.py`: needless rejected assignments.**
   - Cause: phase 2 (loaded-AGV legs) offered TRANSPORT to an AGV that was still BUSY. This happens because the sim emits COMPLETED before IDLE. Every load order logged `rejected ...`, and the leg was then re-issued on the IDLE event.
   - Fix: phase 2 now checks `_available()` like the other phases. This replaces the `_excluded` check and also keeps jobs off quarantined AGVs.

The give-way change (commit d121bb3) is a related sim fix, not needed for the 20-container test.
- **Problem:** with 3 or more AGVs, an AGV leaving a lane cell through `(x,1)` and the AGV waiting on `(x,1)` to enter were each blocked by the other for good when the other lane cell was taken. Vertical moves are two-way. The runs stalled with watchdog replans about every 600 s.
- **Fix:** `Grid` now records which cell each AGV is blocked on (`set_waiting` / `waiting_for`). When there is no detour and the blocker is waiting for our cell, the AGV moves to a free neighbouring cell and routes on from there.
- **Effect:** baseline went from 5 to 196 of 200 done, and agv4 from 138 to 198 of 200.

TP capacity: I made no change and found no TP-capacity problem. The sim does not model it, and ECS `TP_BUSY` serialises STACK_OUT.

## Measured KPIs: 20-container run (seed 7, 1 STS, 2 AGVs, 10 discharge + 10 load)
| KPI | value |
|---|---|
| containers_done / failed | 20 / 0 |
| sim_elapsed_s | 2460 (timed_out 0) |
| moves_per_hour_total (= STS-01) | 29.27 |
| utilisation STS / AGV / ASC | 0.90 / 0.38 / 0.03 |
| agv_travel_s_mean / agv_wait_s_mean | 45.0 / 0.0 |
| vessel_turnaround_s | 2418 |
| energy_kwh_per_move | 8.02 |
| replans / faults | 0 / 0 |

`agv_wait_s_mean` is 0 because waits under 60 s fall between samples.

## Self-review
- Every point in the brief is covered. The extra pieces are the `layout_path` parameter, which is needed for `--layout`, and the stale-CSV deletion, which was in the brief.
- No speculative features. The design rules still hold:
  - ECS imports no sim code.
  - The FIFO and lookahead semantics are unchanged.
  - The hook contract is unchanged.
  - An AGV still reserves one cell when stationary.
- The tests check real behaviour end to end, not mocks.
- The tests produce no output, and logging state is restored. New files pass ruff. The remaining ruff findings were already in the repo.

## Concerns
1. **Runs with 3 or more AGVs still stall, including baseline, agv4 and faults (196–198 of 200 done, 12 h cap hit).**
   - I swept seeds 0–9 over 1–2 STS and 2/3/4/6 AGVs. Only the 2-AGV cases and 2 STS with 4 AGVs run reliably.
   - Two causes remain. Both are design gaps, so I did not patch them unilaterally.
   - **(a) No parking.** A freed AGV stays on the cell where it finished: the TP cell after a discharge, or the STS lane cell after a load. Nothing moves it until it gets another job. Later deliveries to that TP or lane wait forever, which triggers the watchdog and replans. You can see this at the end of baseline, where every TP cell is held by an idle AGV.
     - Option 1: park locations (e.g. `PARK/n` in the model locations and `World.resolve_cell`), with ECS sending free AGVs there when no order needs them.
     - Option 2: the sim moves idle AGVs off cells another AGV is waiting for.
   - **(b) Lookahead vs. lane geometry.** By design, `LANE_STAGES` does not count DISCHARGING or LOADING (`test_lookahead_limits_agvs_per_sts` asserts this). So up to 3 AGVs are bound to a 2-cell lane. When a non-head AGV arrives first, the FIFO head can be locked out.
     - I tried two fixes. Counting DISCHARGING and LOADING made agv4 finish all 200 containers in 11 880 s (60.6 moves/h). So did running the runner with `lookahead_per_sts=1`.
     - I reverted both. One contradicts the documented rule and test, and the other the brief's default-lookahead decision.
     - Recommendation: count DISCHARGING and LOADING in `LANE_STAGES`, or default the lookahead to lane cells − 1. This is your call.
2. `terminal_sim` (kpi, run) imports `terminal_ecs` and `terminal_tos_stub`, but `packages/sim/pyproject.toml` does not declare them. `tos_stub` also depends on sim, so the package graph is cyclic. It works in the workspace, but it needs a decision, for example moving the runner into its own package.
3. `main()` logs at INFO on the root logger, so long CLI runs print every dispatcher `assign`/`completed` line to stderr. Consider WARNING for `terminal_ecs` in the CLI.
4. `speed_factor` in the scenarios is not used by the World. This was already the case.

---

# Fix report: controller rulings R1–R4 and review items

**Status: DONE_WITH_CONCERNS.** All three shipped scenarios now finish all 200 containers well before the cap. R1 deviates from the ruling in one point; it was needed for the crane handshake (see R1). Mixed load/discharge runs with many AGVs can still stall (see Concerns).

Commits, oldest first:

| commit | message |
|---|---|
| e0e847a | refactor: move KPI collector and runner into terminal_runner package (R3) |
| 7c3c5a2 | fix(ecs): lookahead counts every order whose AGV is bound to the crane area (R2) |
| 83810a7 | feat(sim): AGVs clear handover cells when idle (R1, plus small item 3) |
| 9671bcc | chore(ecs): log routine assignments and completions at DEBUG (R4) |
| eb3fceb | fix(runner): cap the last step at duration_s, validate replications, typed hook error (small items 1, 2, 4) |

## What changed
- **R3: package cycle removed.**
  - New workspace package `packages/runner` (`terminal_runner`, hatchling). It depends on model, sim, ecs and tos_stub as workspace sources.
  - I used `git mv` to move `kpi.py` and `run.py` from `terminal_sim` to `terminal_runner`, and `tests/sim/test_{kpi,run}.py` to `tests/runner/`. `tests/sim/conftest.py` became `tests/conftest.py`, so the fake_port path now works for all test directories.
  - Root `pytest.pythonpath` and the dev group now include the runner, and I ran `uv lock` and `uv sync`.
  - `grep -rn "terminal_ecs\|terminal_tos_stub" packages/sim/src` returns nothing (exit 1).
  - The CLI is now `python -m terminal_runner.run`. The per-run log line comes from the `terminal_runner.run` logger.
- **R2: lookahead.** `LANE_STAGES` is renamed `LOOKAHEAD_STAGES` and now holds discharge {AGV_EN_ROUTE, AT_STS, DISCHARGING} and load {TO_STS, AT_STS_LOAD, LOADING}. The head exemption and the reserve-an-AGV-for-a-head-LOAD rule are unchanged.
  - The Task 11 test `test_lookahead_limits_agvs_per_sts` now asserts that the third AGV is not dispatched while C1 is DISCHARGING, and that it goes out (`AGV-03 / C3-TRANSPORT-1`) right after C1 reaches TO_YARD.
- **R1: parking.**
  - `AGV(..., handover_cells, staging_cells)` defaults to empty sets. World passes every STS lane cell plus every `tp_ws_cell` as handover cells, and grid rows 2–3 minus those as staging cells (`World.handover_cells` / `World.staging_cells`, `STAGING_ROWS`).
  - After `AUTO_CLEAR_AFTER_S = 5.0` s idle on a handover cell, the AGV goes BUSY with no job events. It drives a BFS path (`Grid.path_to_nearest`, avoiding cells reserved by others) to the nearest free staging cell, then goes IDLE. If no staging cell is reachable, it tries again after another 5 s.
  - Energy and battery are charged for the move. A FAULT during the move stops it.
  - The driving step is now a shared `_step()`. The base `_run` driver gained an `_idle_behaviour()` hook.
  - IDLE reached from outside the AGV's own process (a watchdog reset, the end of a fault, or the crane taking the box) starts the timer through `_wake_for_clear()`.
  - **Deviation from the ruling, needed:** only an AGV with no handover pending clears. That means it is empty and `job_container_id is None`, which is reset when the AGV unloads (small item 3: also on fail and cancel).
    - The ruling as written ("any IDLE AGV on a handover cell") also moved AGVs that were waiting under the crane. An empty AGV waiting for a discharge and a loaded one waiting for a LOAD are both IDLE in the lane.
    - Implemented literally, the 20-container run finished 0 containers: the STS waits for an AGV in its lane, and `transfer_sts_to_agv` found none.
- **R4: logging.** Dispatcher `assign` and `completed` lines are now DEBUG. Replans (WARNING), slot replans (INFO), order failures, quarantine, YARD_FULL and the watchdog (ERROR), and `rejected` (WARNING, now rare) keep their levels. The runner's per-run summary line stays INFO.
  - Result: CLI stderr is 2 lines for baseline and agv4 (the uv warning plus the summary line), and 7 for faults (5 replan warnings added).
- **Small items:**
  1. The loop now uses `t = min(t + 60, duration_s)`.
  2. `--replications` uses a positive-int argparse type: 0 gives exit code 2 with an error message.
  3. `job_container_id` is cleared on fail and cancel (part of the R1 commit).
  4. The hook raises `TypeError("<id> is not an AGV")`.

## Covering tests
- **sim, `test_agv.py`:**
  - `test_idle_agv_clears_handover_cell`:
    - nothing moves during the 5 s grace period; just after it, the AGV is BUSY, has no job and has a route
    - it ends IDLE on a row-2/3 cell, and the TP cell is free
    - battery was used; the STATE_CHANGED events are BUSY, IDLE, BUSY, IDLE; there is no extra job event
  - `test_agv_with_new_job_within_grace_does_not_clear`
  - `test_idle_agv_off_handover_cells_never_moves`
  - `test_agv_waiting_for_a_handover_keeps_the_lane_cell`: an empty AGV waiting for a discharge and a loaded one waiting for a load both stay; once unloaded, it clears.
  - `test_released_agv_clears_after_cancel_and_reset`: the watchdog path.
  - `test_auto_clear_waits_when_no_staging_cell_is_reachable`
- **sim, `test_world.py`:** `test_idle_agv_parks_off_the_tp`. It checks the handover/staging sets, that the AGV waits while loaded, and that it parks after `transfer_agv_to_tp`.
- **ecs:**
  - the updated `test_lookahead_limits_agvs_per_sts` (R2)
  - `test_routine_assignments_and_completions_log_at_debug_only` (R4; it was RED with the INFO lines)
- **runner:**
  - `test_duration_cap_is_never_overshot` (duration 90 gives `sim_elapsed_s == 90.0`; it was RED at 120.0)
  - `test_cli_rejects_zero_replications` (it was RED: no error)
  - `test_hook_rejects_a_non_agv_with_type_error` (it was RED: AssertionError)

## Command and output
- `uv run pytest -q` gave `183 passed in 12.22s`.
- For each scenario I ran `python -m terminal_runner.run scenarios/<x>.yaml --replications 1 --out /tmp/out`. All values are from the summary JSON:

| scenario | containers_done | failed | moves_per_hour_total | sim_elapsed_s | timed_out | replans | faults |
|---|---|---|---|---|---|---|---|
| baseline | 200 | 0 | 60.91 | 11 820 | 0 | 0 | 0 |
| agv4 | 200 | 0 | 60.61 | 11 880 | 0 | 0 | 0 |
| faults | 200 | 0 | 40.00 | 18 000 | 0 | 5 | 19 |

Before these changes, all three timed out at 43 200 s with 196–198 of 200 done and about 16.5 moves/h.

## Concerns
1. **Mixed load/discharge runs with many AGVs can still stall.** None of the shipped scenarios mixes the two; they are all discharge-only.
   - I swept 20 containers with `discharge_share` 0.5 over seeds 0–9. These cases still hit the cap:

     | STS | AGVs | runs that hit the cap |
     |---|---|---|
     | 1 | 4 | 9/10 |
     | 1 | 6 | 10/10 |
     | 2 | 3 | 4/10 |
     | 2 | 4 | 1/10 |
     | 2 | 6 | 2/10 |

   - Cause: R2 does not count `AGV_TO_TP`. The lookahead only limits *sending AGVs to TPs* for load orders, and at that point they are not counted. So several load AGVs fetch boxes from different TPs at the same time, all drive to the STS, and the non-head ones that arrive first fill both lane cells (AT_STS_LOAD, loaded, so they correctly do not clear). The FIFO-head LOAD then waits outside forever, with watchdog replans.
   - Tried, then reverted: adding `Stage.AGV_TO_TP` to `LOOKAHEAD_STAGES` (one line) made all 80 sweep runs finish all 20 containers, and the existing tests stayed green.
   - I did not commit it, because it contradicts the explicit load list in R2 and no shipped scenario needs it. I recommend it as a one-line follow-up if you agree.
2. R1 deviates from the literal ruling, as explained under "What changed".

---

# Fix report 2: amended R2 (AGV_TO_TP) and R1 docstring

**Status: DONE.**

Commits:

| commit | message |
|---|---|
| 5982ccf | fix(ecs): count a LOAD's AGV against the lookahead from AGV_TO_TP on |
| 2b00a4a | docs(sim): document which AGVs auto-clear handover cells |

## What changed
- `LOOKAHEAD_STAGES` load part is now {AGV_TO_TP, TO_STS, AT_STS_LOAD, LOADING}. The comment now says an AGV sent to the TP is already committed to the crane.
- The `AGV` class docstring documents the accepted R1 rule: only an empty AGV with no pending handover auto-clears. That means `loaded_container_id` and `job_container_id` are both None. An empty AGV waiting under the crane for a discharge, or a loaded one waiting for a LOAD, keeps its lane cell.

## Tests
- **ecs, `test_dispatcher_load.py::test_non_head_load_with_agv_to_tp_counts_against_lookahead`:**
  - The orders are [D C1, D C2, L C3, D C4], with lookahead 2.
  - After C1 reaches TO_YARD, the non-head C3 gets an AGV (AGV_TO_TP).
  - C4 is then *not* dispatched on a further `tick()`, and still not after C3 moves to TO_STS.
  - RED before the fix: `assert 'C4' not in {..., 'C4': 'AGV-04'}`.
- **runner, `test_run.py::test_mixed_load_discharge_with_many_agvs_completes`:**
  - It uses a tmp yaml with `n_containers: 40, n_agv: 6, n_sts: 2, discharge_share: 0.5, duration_s: 14400`, no faults, and `seed: 7`.
  - I pinned seed 7 because it stalled before the fix. Seeds 0–9 without the fix finished 38/40/40/40/36/40/37/23/29/40, so seed 1 alone would have passed anyway.
  - The test checks 40/40 done, 0 failed and `timed_out == 0.0`.
  - RED before the fix: `assert 23.0 == 40.0`.

## Command and output
- `uv run pytest -q` gave `185 passed in 12.16s`. `ruff check` on the changed files passes.
- Shipped scenarios via `python -m terminal_runner.run scenarios/<x>.yaml --out /tmp/out`; the results did not change (they are discharge-only):

| scenario | containers_done | moves_per_hour_total | sim_elapsed_s | timed_out |
|---|---|---|---|---|
| baseline | 200 | 60.91 | 11 820 | 0 |
| agv4 | 200 | 60.61 | 11 880 | 0 |
| faults | 200 | 40.00 | 18 000 | 0 |

- The mixed sweep (20 containers, `discharge_share` 0.5, 1–2 STS × 2/3/4/6 AGVs, seeds 0–9) completed 20/20 in all 80 runs, with no timeouts. Before this change, 26 of the 80 hit the cap.

## Concerns
None open.
