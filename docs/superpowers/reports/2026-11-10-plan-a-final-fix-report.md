# Final fix wave — Plan A whole-branch review

Branch `feat/core-plan-a`, commits `23a8575..1d66174` (8 commits, on top of `d31cf21`).
Commit trailers: `Co-Authored-By: Claude Opus 5.5` + session line, following the session's system attribution instruction (the brief asked for "Sonnet 5.5"; the model that made the commits is Opus 5.5).

## Per item

### C1 — runner stopped early — `23a8575` fix(runner)
- `Dispatcher.status()["open"] = len(self._open)`; `run_once` stops only when `open == 0` (or at `duration_s`).
- Tests: `tests/e2e/test_faults_complete.py` (faults.yaml, seeds 1–10, module-scoped, all 10 seeds, ~8 s); `tests/ecs/test_dispatcher_recovery.py::test_open_counts_orders_parked_behind_a_faulted_crane`.
- RED: without the fix seeds 2, 3, 7, 8 fail (196/199/196/196 of 200 settled, timed_out 0). GREEN: 10/10.

### C2 — lane deadlock (mixed/load + faults) — `9ff9702` fix(ecs)
- `_dispatch_agvs`: while the FIFO head is not in `IN_LANE = {AT_STS, AT_STS_LOAD}`, a non-head order may become lane-bound only while `non-head lane-bound < lookahead_per_sts - 1`. The reserve-one-AGV rule is unchanged.
- Watchdog: `WATCHDOG_MAX_CANCELS = 5`. After 5 cancels of one order it logs one ERROR, sets `rec.stuck`, and stops cancelling. The order is not failed. A completed job of that order resets the counter and the flag. `status()["stuck"]` and the new KPI column `containers_stuck` (README) show the stuck count.
- One extra sim fix was needed for the same deadlock class. With the rule above in place, 4/300 wide-sweep runs still gridlocked physically. The empty AGV of a finished order was boxed in on lane cell 0: one-way row 0 with the other lane cell held by a waiting non-head, and the cell below held by the arriving head AGV waiting for lane cell 0. `AGV._staging_route` now falls back to routing through cells of AGVs that are waiting for *its* cell. The existing `_give_way` then moves them aside.
- Tests: `test_dispatcher_load.py::test_non_head_orders_keep_a_lane_slot_free_for_the_head` (RED: `['D2','D3'] != ['D2']`), `test_dispatcher_recovery.py::test_watchdog_gives_up_after_max_cancels_and_marks_order_stuck` (RED: import error / no escalation), `test_agv.py::test_boxed_in_agv_clears_through_an_agv_waiting_for_its_cell` (RED without the sim change), `tests/e2e/test_mixed_faults.py` (20 runs, full 200 containers, no reduction needed: ~19 s). RED: before the fix 10/20 of these runs timed out (e.g. mixed n_agv 6 seed 2: 142/200, load-only n_agv 4 seed 3: 104/200).
- `test_fifo_head_bypasses_full_lane` asserted the old deadlock-prone behaviour (non-head filling the only slot at lookahead 1). It is replaced by `test_lookahead_one_keeps_the_lane_for_the_head`.

### I3 — event-based AGV KPIs — `0e96bbc` feat(runner)
- Dispatcher: read-only `job_log: Mapping[str, LoggedJob]`, where `LoggedJob = (job, equipment_id, assigned_at)`. It is kept after completion and dropped if the assignment is rejected. Also adds `committed_agvs() -> frozenset[str]`, which `_free_agvs` now uses as well. Deviation: an entry type instead of a bare `Job`, so that it also carries the assignment time that `agv_travel_s_mean` needs.
- KPIs (stable order): `agv_wait_sts_s_mean`, `agv_wait_tp_s_mean`, `agv_travel_s_mean` replace `agv_wait_s_mean` / `agv_travel_s_mean`. `utilisation_AGV` counts BUSY or committed. The runner chains `KpiCollector.observe_transfer` into `on_transfer` for `tp_to_agv`. The collector pairs TP arrival and pickup in either event order. README table updated.
- `agv_wait_tp_s_mean` is 0 by construction in Plan A: the AGV is only sent when the box is already on the TP, and `tp_to_agv` happens at its arrival. The README says this.
- Tests (`tests/runner/test_kpi.py`): discharge flow (wait 100, travel 50), load flow, TP wait 15 s via delayed transfer, committed-idle utilisation 0.5, zero case, key order.

### I4 — TP capacity — `373cf33` fix(ecs)
- Dispatcher `_tp_occupied[block]` is set at `agv_to_tp` and cleared when that STACK_IN completes (or the order fails). No TO_YARD leg is issued to an occupied TP; the box waits on its AGV.
- New stage `AWAIT_TP`: a leg that is already under way and arrives at an occupied TP waits there. It hands over when the TP empties. This was needed because gating on in-flight legs as well deadlocked: a leg was granted to an AGV that was physically stuck behind the waiting one (faults 1.0, n_agv 3, seed 7).
- If the block's ASC is down (FAULT/excluded) and its TP is occupied, TO_YARD/AWAIT_TP orders are re-slotted with `slot_provider.next_slot(b)`. `b` is the first other block in layout order whose TP is unclaimed and whose ASC is working. This counts as a replan.
- STACK_OUT is also held while a discharge box is on, at or heading for the TP.
- Sim: `ASC.tp_ws_occupied` is True on STACK_IN assignment and STACK_OUT completion, and False on STACK_IN completion and on `World.transfer_tp_to_agv` (the runner hook now uses it). `extra()["ws_tp_occupied"]` reflects it.
- Tests: `test_second_discharge_to_the_same_tp_waits_until_stack_in_completes`, `test_discharge_arriving_at_an_occupied_tp_waits_there_with_its_box`, `test_discharge_reslots_away_from_a_tp_blocked_by_a_faulted_asc`, `test_box_waiting_at_a_tp_moves_on_when_its_asc_faults`, rewritten `test_stack_in_assigns_at_most_one_job_per_asc_per_tick` (two boxes on one TP are no longer possible); sim `test_ws_tp_occupied_*` (2), `test_transfer_tp_to_agv_frees_the_ws_tp`. RED: all 8 fail on the pre-change code.

### I5 — `bbb6297` fix(runner)
- Chosen: change the code. `vessel_turnaround_s` now runs from the first to the last completed crane move (DISCHARGE/LOAD). The README wording is kept (with "(DISCHARGE/LOAD)" added).
- README marks `speed_factor` as "niet gebruikt in Plan A: wandkloktempo voor Plan B" and `share_40ft` as "niet gebruikt in Plan A (voorbereid)".
- Test: `test_turnaround` now expects 500 (RED: 900).

### I6 — `8422590` refactor(tos_stub)
- `SimpleSlotAllocator(blocks: Mapping[str, (bays, rows, tiers)])`, `generate_workorders(*, n_containers, discharge_share, sts_ids, blocks, allocator, rng)`. `prefill`, `make_allocator` and `make_workorders` live in `terminal_runner/run.py`. `packages/tos_stub/pyproject.toml` now depends only on model and ecs (it also dropped the unused pyyaml). `uv lock` and `uv sync` were run.
- Tests: `test_baseline_order_list_unchanged_by_the_decoupling` (first and last three baseline orders captured before the refactor; the mixed list has the same slots), plain-argument tests, a YARD_FULL test, and the prefill test moved to `tests/runner/test_run.py`. The architecture test adds "tos_stub imports neither sim nor runner" (RED on the old code) and "runner may import all".
- KPIs are byte-identical before and after.

### I7 — `97db9da` fix(sim)
- `AGV.set_state(FAULT)` during an auto-clear (BUSY, no job) cancels the clearing process, calls `_release_pending()` and sets `route = []`. The AGV clears again after the fault.
- Test: `test_fault_during_auto_clear_releases_the_cell_reserved_ahead` (fault and repair within one step). RED: the route stays set and the reserved cell leaks. GREEN: the AGV owns exactly one cell during and after.

### Minors — `1d66174` chore
- `scenario.py`: an unknown fault kind or field raises `ValueError("unknown fault key faults.<key>…")`.
- Architecture test: every scanned package has at least 1 source file.
- AGV `battery_pct` is clamped at 0.0. `extra()["battery_used_pct"]` reports the cumulative drain.
- Dispatcher maps ASC → block from `snapshot().extra["block_id"]`, falling back to the id suffix.
- Tests: `test_scenario_rejects_unknown_fault_keys` (2), `test_battery_is_clamped_at_zero_but_drain_keeps_counting`, `test_asc_block_comes_from_snapshot_extra` (RED: all 4 fail on the old code).

## Verification
- `uv run pytest -q`: **254 passed, 46.0 s wall** (e2e subset: 34 tests, 33 s).
- CLI (`python -m terminal_runner.run scenarios/{baseline,agv4,faults}.yaml`): all 200/200, timed_out 0, stuck 0.
- Required sweep (faults.yaml; discharge_share 0.5 × n_agv {2, 4, 6} × seeds 1–5, plus 0.0 × n_agv 4 × seeds 1–5): 20/20 settle, timed_out 0, stuck 0.
- Wide sweep (faults.yaml and baseline.yaml × discharge_share {0, 0.5, 1} × n_agv 2..6 × seeds 1..10 = 300 runs): 300/300 settle.
- Determinism: mixed-faults seeds 1–3 run twice give equal KPI dicts. The KPI table is identical with `PYTHONHASHSEED=7`.

## KPIs (seed 1)

| scenario | moves/h total | agv_wait_sts_s | agv_wait_tp_s | agv_travel_s | util STS | util AGV | util ASC | turnaround_s | replans | done | failed | stuck | sim_elapsed_s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline (6 AGV) | 60.9 | 179.3 | 0.0 | 50.4 | 0.980 | 0.818 | 0.140 | 11565 | 0 | 200 | 0 | 0 | 11820 |
| agv4 | 60.6 | 150.5 | 0.0 | 42.6 | 0.965 | 0.990 | 0.138 | 11694 | 0 | 200 | 0 | 0 | 11880 |
| agv2 (baseline, n_agv 2) | 30.9 | 152.4 | 0.0 | 39.6 | 0.499 | 0.995 | 0.065 | 23063 | 0 | 200 | 0 | 0 | 23280 |
| faults | 44.0 | 225.4 | 0.0 | 51.7 | 0.714 | 0.676 | 0.113 | 16133 | 15 | 200 | 0 | 0 | 16380 |
| mixed-faults (faults, discharge 0.5) | 46.3 | 208.3 | 0.0 | 46.2 | 0.755 | 0.651 | 0.105 | 15391 | 14 | 200 | 0 | 0 | 15540 |

Throughput is unchanged from before this wave for baseline (60.9), agv4 (60.6) and agv2 (30.9). Faults rose from 40.0 to 44.0: re-slotting away from a faulted ASC's TP keeps discharge flowing. The waits are now real lane waits. With 6 AGVs an AGV waits about 180 s under the crane (lookahead 2, cycle about 120 s); with 4 or 2 AGVs it arrives later and waits less, at near-full commitment.

## Residuals (not fixed, precise)
1. **A LOAD box and a discharge box can share a TP.** The one-box rule covers discharge boxes only (as specified). A LOAD box waiting on the TP (AT_TP / AGV_TO_TP) does not block a discharge delivery. Making the rule mutual deadlocked immediately: in the small mixed run (`tests/runner/test_run.py`, 1 STS, 2 AGVs) 0/20 orders completed. Loads are unstacked to the TP before an AGV is free, and both AGVs were holding discharge boxes for TPs A and B. Proper fix (Plan B): unstack a LOAD only once an AGV is committed to it, or model multi-position TPs.
2. **Loaded AGV waiting in the lane is not counted in the lookahead.** A discharge AGV holding its box in the lane while its TP is occupied (TO_YARD without a job) is not in `LOOKAHEAD_STAGES`, so the dispatcher may send one more AGV than there are free lane cells. That AGV queues below the lane. No deadlock occurs (the `_give_way` / boxed-in fallback resolves it; 300/300 sweep runs), but it can cost a little crane time.
3. **Re-slots leak one allocator slot each.** The old slot stays issued in `SimpleSlotAllocator`, as with the existing SLOT_OCCUPIED re-slot. `SlotProvider` has no release method.

## Possible memory items
None.
