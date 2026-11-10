# Task 3 report: the sim does box handover; the runner sets no transfer hooks

**Status:** DONE_WITH_CONCERNS (one concern, below)

## Commits
- `0532082` test: capture Plan A baseline KPIs as reference. This commit holds `tests/e2e/fixtures/plan_a_baseline_seed1.json`, the full `run_once(baseline, layout, 1)` dict, written before any code change.
- `bc6a5b5` feat(sim): autonomous box handover; runner drops transfer hooks

## What changed
- `world.py`: new `Handover(world)`, exposed as `World.handover`. `World.__init__` subscribes it on `SimEquipmentPort` before any other subscriber, which `test_handover_is_the_first_subscriber` checks. It has:
  - `try_discharge(sts) -> bool`: puts the box on the empty lane AGV whose `job_container_id` matches it.
  - `on_arrival(agv)`: at a TP cell, a loaded AGV unloads and its box is appended to `tp_box[block]`. An empty AGV whose `job_container_id` is in the list takes that box.
  - `on_event`: on JOB_COMPLETED, ASC STACK_OUT appends the box to the list and STACK_IN removes it. STS LOAD empties the lane AGV that carries the box.
  - `tp_box: dict[block, list[str]]`. The ASC's `tp_ws_occupied` is set to `bool(list)`. The sim adds no capacity rule of its own.
  - `World.transfer_*` are kept for tests, and now keep `tp_box` in step.
- `sts.py`: new `on_discharge_done: Callable[[STS], bool]`, default `lambda s: True`. It is called before `complete_job` on a DISCHARGE; False gives `fail_job("NO_AGV_IN_LANE")`.
- `agv.py`: new `on_arrival: Callable[[AGV], None]`, default no-op. It is called at the goal, before `complete_job`.
- `asc.py`: comment only.
- `run.py`: `_transfer_hook` and the `on_transfer` wiring are removed, so `dispatcher.on_transfer` stays None. `packages/ecs` is unchanged; `git diff 0532082 -- packages/ecs` is empty.
- `kpi.py`: `observe_transfer` is removed. `agv_wait_tp_s_mean` is now computed in `_tp_wait()`: each completed empty TRANSPORT to `TP/..` (a LOAD leg) is paired with the next TRANSPORT assigned to the same AGV in `dispatcher.job_log`, and the wait is that assignment's `assigned_at` minus the arrival time.

## Tests
- New `tests/sim/test_handover.py` (8 tests): the 5 from the brief, plus:
  - Handover is the first subscriber.
  - An AGV sent for another box does not take the one on the TP.
  - Two boxes on one TP (a LOAD box plus a discharge box) form a FIFO list, and the flag drops only when the list is empty.
- New `tests/e2e/test_plan_a_equivalence.py`.
- Updated tests:
  - `test_kpi.py`: the LOAD test no longer uses a hook. The TP-wait tests now use a stand-in that has only a job log.
  - `test_run.py`: the hook test is replaced by an assertion that `on_transfer is None`.
  - `test_world.py::test_idle_agv_parks_off_the_tp`: a loaded AGV now gives its box to the TP on arrival, so the "waits with box on board" case is checked at the STS lane instead.
- `uv run pytest -q`: **278 passed** (62.7 s).

## Equivalence
`run_once(baseline, layout, 1)` matches the fixture **exactly on every key, `agv_wait_tp_s_mean` included** (0.0, because baseline has no LOAD orders). As an extra check I ran Plan A (stashed) against the new code on `agv4.yaml`, `faults.yaml` and the mixed 50/50 scenario (seed 7, 40 containers, 2 STS, 6 AGVs). All KPI dicts were identical, `agv_wait_tp_s_mean` included. No KPI key changed.

## Concern
In Plan A's ECS AWAIT_TP case (a discharge box reaches a TP that still holds another discharge box), the AGV stayed loaded until the ECS called `agv_to_tp`. Spec §3 also says the AGV should wait. Following the task's binding constraint, the sim now unloads at once and records a second box in the FIFO list. The empty AGV may then leave the TP (auto-clear) while the ECS still counts it as committed until the TP is free. None of the scenarios I ran hit this case, since all KPIs came out identical, but it is a real behaviour difference from the spec text.

---

# Fix report: review finding on TP capacity and AWAIT_TP

**Status:** DONE. The concern from the first pass is resolved: the coordinator's ruling restores spec §3.

## What changed (`packages/sim/src/terminal_sim/world.py`)
- `tp_box[block]` is now a list of `TpBox(cid, origin)`. `origin` is `"discharge"` (put down by an AGV) or `"stack_out"` (put down by the ASC). `Handover.boxes(block)` returns the container ids.
- `on_arrival`: if a loaded AGV reaches a TP that already holds a **discharge** box, the AGV keeps its box on board and stays on the TP cell. Because it is loaded, it does not auto-clear. A LOAD (`stack_out`) box on the TP does not block a discharge box, which matches the Plan A rule that a LOAD box and a discharge box may share a TP.
- STACK_IN completion: the box is removed, then `_release_waiting_agv` unloads any loaded, job-less AGV standing on that `tp_ws_cell` into `tp_box`. Handover is the first subscriber, so this happens before the ECS's AWAIT_TP → `_deliver_to_tp` runs in the same tick.
- New public `Handover.put_on_tp(cell, cid, origin="discharge")` and `Handover.take_from_tp(cell, cid)`. `World.transfer_agv_to_tp` and `World.transfer_tp_to_agv` now use them.
- A LOAD that completes with no lane AGV carrying the box is now logged with `log.error` (it was a warning).

## Tests covering the fix
- `test_second_discharge_box_waits_on_its_agv_until_stack_in_completes`:
  - the second AGV keeps D2, stays IDLE on the TP cell and is not auto-cleared;
  - when the STACK_IN of D1 completes, a later subscriber already sees `boxes("A") == ["D2"]` and an empty AGV;
  - the ASC flag is still True afterwards.
- `test_load_box_on_tp_does_not_block_a_discharge_box`: the origin tags come out as `[("L1","stack_out"), ("D1","discharge")]`.
- The existing tests were switched to `boxes("A")`.

## Commands and output
- `uv run pytest -q tests/sim/test_handover.py tests/e2e/test_plan_a_equivalence.py`: 11 passed
- `uv run pytest -q`: **280 passed** in 64.07 s

## Plan A comparison
- Plan A was run from `git worktree add /tmp/plan-a master` (a20d46c), using `uv run` inside the worktree. I checked that the imports came from `/tmp/plan-a` and that `_transfer_hook` is present there.
- The same script ran on this branch. Both worktrees were removed afterwards.
- Every KPI key is identical in all five runs, so no difference needs explaining.

| run | side | moves_per_hour_total | agv_wait_sts_s_mean | agv_wait_tp_s_mean | agv_travel_s_mean | energy_kwh_per_move | replans | faults | containers_done | containers_failed | containers_stuck | sim_elapsed_s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline s1 | Plan A (master) | 60.9137 | 179.329 | 0 | 50.3858 | 8.028 | 0 | 0 | 200 | 0 | 0 | 11820 |
| baseline s1 | B1 Task 3 fix | 60.9137 | 179.329 | 0 | 50.3858 | 8.028 | 0 | 0 | 200 | 0 | 0 | 11820 |
| faults s1 | Plan A (master) | 43.956 | 225.399 | 0 | 51.7425 | 8.0352 | 15 | 17 | 200 | 0 | 0 | 16380 |
| faults s1 | B1 Task 3 fix | 43.956 | 225.399 | 0 | 51.7425 | 8.0352 | 15 | 17 | 200 | 0 | 0 | 16380 |
| faults s3 | Plan A (master) | 51.9481 | 206.652 | 0 | 50.4083 | 8.02236 | 14 | 17 | 200 | 0 | 0 | 13860 |
| faults s3 | B1 Task 3 fix | 51.9481 | 206.652 | 0 | 50.4083 | 8.02236 | 14 | 17 | 200 | 0 | 0 | 13860 |
| mixed s7 | Plan A (master) | 60 | 159.224 | 0 | 45.6167 | 7.9728 | 0 | 0 | 40 | 0 | 0 | 2400 |
| mixed s7 | B1 Task 3 fix | 60 | 159.224 | 0 | 45.6167 | 7.9728 | 0 | 0 | 40 | 0 | 0 | 2400 |
| agv4 s1 | Plan A (master) | 60.6061 | 150.489 | 0 | 42.5725 | 7.98096 | 0 | 0 | 200 | 0 | 0 | 11880 |
| agv4 s1 | B1 Task 3 fix | 60.6061 | 150.489 | 0 | 42.5725 | 7.98096 | 0 | 0 | 200 | 0 | 0 | 11880 |

**The first pass did diverge.** I checked the first-pass commit `bc6a5b5` in a temporary worktree. It differed from Plan A on faults seed 3:

| KPI | Plan A | bc6a5b5 |
|---|---|---|
| `agv_wait_sts_s_mean` | 206.652 | 206.685 |
| `agv_travel_s_mean` | 50.4083 | 50.3917 |
| `energy_kwh_per_move` | 8.02236 | 8.02212 |

The cause was the early unload during AWAIT_TP, after which the AGV auto-cleared. On faults seed 1, `bc6a5b5` matched Plan A on every KPI key, so this KPI check does not detect the ghost-box path the review describes for that seed. The fix puts faults seed 3 back to exact equality.

**Remaining edge case, not exercised by any scenario:** if the ECS fails an order whose discharge box physically stays on a TP, the ECS frees its own TP record while the sim keeps the box. A later discharge AGV at that TP would then wait on board until a STACK_IN completes there.
