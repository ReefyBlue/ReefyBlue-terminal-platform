# Task 12 report: ECS load path, replanning, watchdog

**Status:** DONE (with the deviations and concerns listed below)
**Branch:** feat/core-plan-a
**Commits:**
- `ea0c6bc refactor(ecs): move order stages and record into orders module`. Behaviour is unchanged; 19/19 ECS tests and 124/124 full suite were green before this commit.
- `e5658aa feat(ecs): load flow, replanning on failure and fault, AGV watchdog`

## What was implemented

### Refactor
- New `packages/ecs/src/terminal_ecs/orders.py` (99 lines). It holds:
  - `Stage`
  - `OrderRecord` (renamed from `_OrderRecord`, with a new `submit_seq` field)
  - the stage sets `LANE_STAGES`, `PRE_CRANE`, `NEEDS_AGV`, `TP_BUSY`, `PENDING`
  - the rollback table `ROLLBACK` / `RELEASE_AGV`
  - the pure helper `valid_slot()`
- `dispatcher.py` (415 lines) keeps `Dispatcher`: port I/O, tick phases, event handling, recovery and watchdog.
- It is slightly over the ~400-line guide. I did **not** split out `recovery.py`. The recovery code (`_replan`, `_on_state`, `_watchdog`, `_reslot`, `_detach`) is about 60 lines and is tightly bound to dispatcher state (`_job_eq`, `_excluded`, `_by_job`). Splitting it would need a mixin or passing most of the dispatcher's state around. If the runner work grows the file further, move those five methods to a `RecoveryMixin` in `recovery.py`.

### Load path
- A LOAD order starts in `AWAIT_ASC`.
- `STACK_OUT` is issued per idle ASC, at most one ASC job per pass. STACK_IN goes first because it clears the TP.
- STACK_OUT waits are ordered by `seq`, which equals submit order unless the order was re-staged.
- STACK_OUT is gated per block. No new STACK_OUT is issued while another order of that block is in `UNSTACKING`, `AT_TP` or `AGV_TO_TP`; the TP holds one box.
- STACK_OUT ignores the lookahead.
- In `AT_TP`, the nearest available AGV gets `TRANSPORT (agv -> TP/<b>/WS)`. "Nearest" is measured to the block's **ASC position**, because the port gives no position for a TP.
- When that completes: `on_transfer("tp_to_agv", agv, cid)`, then `TRANSPORT (TP -> sts)` on the same AGV, then `AT_STS_LOAD`.
- The STS issues `LOAD (sts -> VESSEL)` only to its FIFO head.
- When the LOAD completes: `on_transfer("agv_to_sts", agv, cid)`, the AGV is released, and the order is DONE.
- `STACK_OUT` failing with `SLOT_EMPTY` sets the order to FAILED with reason `SLOT_EMPTY` (`failed += 1`). There is no retry.

### Combined per-STS FIFO
- `_fifo_head(sts)` is the earliest submitted order for that STS, DISCHARGE or LOAD, whose stage is in `PRE_CRANE`.
- Only the head may use the crane.

### Replanning
- Any `JOB_FAILED` that is not (`STACKING` + `SLOT_OCCUPIED`) and not (`UNSTACKING` + `SLOT_EMPTY`) calls `_replan`. `_replan` does the following:
  - detaches the job (from `jobs`, `_by_job` and `_job_eq`)
  - adds the equipment to `_excluded`
  - increments `replans`
  - applies the rollback table
  - resets `seq` to `submit_seq`, so the order keeps its TOS position
- `STATE_CHANGED -> FAULT` excludes the equipment and calls `_replan` for its running job, if it has one.
  - The sim's following `JOB_FAILED(FAULT)` finds no record and is ignored, which makes the pair idempotent.
  - Job ids are never reused (suffix counter), so a late event can never match a re-issued job.
- `STATE_CHANGED -> IDLE` clears the exclusion.
- Excluded and quarantined equipment is never chosen: `_available()` is used for STS, ASC and AGV choices, and bound-AGV re-issues also check `_excluded`.

### Watchdog
Runs at the start of `tick()`, once per call:
- An AGV makes progress when an assignment is issued to it, when any event arrives from it, or when its `snapshot().position` changes.
- An AGV with a running job and `now - last_progress > watchdog_s` is handled in this order:
  1. `_replan` (rollback first)
  2. `port.cancel_job`
  3. `port.reset`
- Because the rollback comes first, the synchronous `JOB_FAILED(CANCELLED)` from the sim is ignored and not counted twice. A test uses a port that emits CANCELLED inside `cancel_job`.

### TRANSFER_ERROR
- The order is FAILED, and the AGV that may hold the box is added to `_quarantined`, permanently. `IDLE` does not lift the quarantine.
- This applies to every transfer kind that involves an AGV, which is all four.
- `status()["equipment"]` still shows the port's state.

### YARD_FULL
- Unchanged. The existing `test_slot_occupied_and_yard_full_marks_failed` and `test_invalid_slot_from_provider_is_treated_as_yard_full` cover it, so no new test was added.

## Updated state machine (for Task 15)

`job` = `current_job_id`; `None` means "tick() still has to issue this stage's job". Only `tick()` calls `assign_job`; event handlers only move stages.

```
DISCHARGE
AWAIT_AGV(None)   --TRANSPORT-n (agv->STS)-->      AGV_EN_ROUTE(job)  --done--> AT_STS(None)
AT_STS(None)      --DISCHARGE-n (VESSEL->STS)-->   DISCHARGING(job)   --done, on_transfer("sts_to_agv", STS, cid)--> TO_YARD(None)
TO_YARD(None)     --TRANSPORT-n (STS->TP/b/WS)-->  TO_YARD(job)       --done, on_transfer("agv_to_tp", AGV, cid), AGV freed--> STACKING(None)
STACKING(None)    --STACK_IN-n (TP->slot)-->       STACKING(job)      --done--> DONE
STACKING(job)     --SLOT_OCCUPIED--> new valid slot: STACKING(None), replans+=1 | none/invalid: FAILED(YARD_FULL)

LOAD
AWAIT_ASC(None)   --STACK_OUT-n (slot->TP/b/WS)--> UNSTACKING(job)    --done--> AT_TP(None)
AT_TP(None)       --TRANSPORT-n (agv->TP/b/WS)-->  AGV_TO_TP(job)     --done, on_transfer("tp_to_agv", AGV, cid)--> TO_STS(None)
TO_STS(None)      --TRANSPORT-n (TP/b/WS->STS)-->  TO_STS(job)        --done--> AT_STS_LOAD(None)
AT_STS_LOAD(None) --LOAD-n (STS->VESSEL)-->        LOADING(job)       --done, on_transfer("agv_to_sts", AGV, cid), AGV freed--> DONE
UNSTACKING(job)   --SLOT_EMPTY--> FAILED(SLOT_EMPTY)
```

`on_transfer(kind, equipment_id, cid)` kinds: `sts_to_agv` (eid = STS), `agv_to_tp` (eid = AGV), `tp_to_agv` (eid = AGV), `agv_to_sts` (eid = AGV). If the hook raises, the order goes to FAILED(TRANSFER_ERROR) and the AGV is quarantined.

### Rollback on JOB_FAILED (other reasons), STATE_CHANGED->FAULT with a running job, or the watchdog

Every case also does `replans += 1` and excludes the equipment until `STATE_CHANGED -> IDLE`.

| failed in | goes to | AGV | re-issued by tick() |
|---|---|---|---|
| AGV_EN_ROUTE | AWAIT_AGV | released | new AGV, `TRANSPORT-(n+1)` |
| DISCHARGING | AT_STS | stays (in lane) | `DISCHARGE-(n+1)` once STS available |
| TO_YARD | TO_YARD | **bound** (box on AGV) | `TRANSPORT-(n+1)` on same AGV once IDLE |
| STACKING | STACKING | — | `STACK_IN-(n+1)` once ASC available |
| UNSTACKING | AWAIT_ASC | — | `STACK_OUT-(n+1)` |
| AGV_TO_TP | AT_TP | released | new AGV, `TRANSPORT-(n+1)` |
| TO_STS | TO_STS | **bound** (box on AGV) | `TRANSPORT-(n+1)` on same AGV once IDLE |
| LOADING | AT_STS_LOAD | stays | `LOAD-(n+1)` |

- A rolled-back order gets `seq = submit_seq`. It stays FIFO head if it was head, and it goes to the front of waiting queues.

### tick() phases
0. watchdog
1. crane jobs for the FIFO head of each available STS
2. bound-AGV transports (TO_YARD, TO_STS)
3. ASCs: STACK_IN, then STACK_OUT (one job per ASC per pass, TP gate)
4. empty AGVs per STS for `AWAIT_AGV` and `AT_TP`, in submit order:
   - the lane count is **combined** over `LANE_STAGES` = {AGV_EN_ROUTE, AT_STS, AGV_TO_TP, TO_STS, AT_STS_LOAD}
   - the FIFO head is exempt from the lookahead

### AGV reservation
- An AGV is reserved while some open record has `agv_id == agv`, plus any excluded or quarantined AGV.
- Release points: the box reaches the TP (discharge), the LOAD completes (load), the order fails, or a rollback from AGV_EN_ROUTE / AGV_TO_TP.

## Deviations / decisions to review
1. **Job id after a fault is `C1-TRANSPORT-2`, not "a new TRANSPORT-1".**
   - The brief's `test_fault_during_transport_replans` text says "new TRANSPORT-1 … on AGV-02".
   - I kept the suffix counter, so ids are never reused. In the sim the FAULT state change comes *before* the `JOB_FAILED`. If ids were reused, that late `JOB_FAILED(C1-TRANSPORT-1)` would hit the re-issued job on AGV-02.
   - The test asserts the same leg (`AGV-02 -> STS-01`) with id `C1-TRANSPORT-2`.
   - This matches the brief's own `-TRANSPORT-3` expectation for the TO_YARD re-issue.
2. **Combined lane lookahead plus head exemption.**
   - The lookahead is counted over discharge and load lane stages together, per STS.
   - The FIFO head may exceed the lookahead. Without the exemption the STS deadlocks: later DISCHARGE AGVs fill the lane while the head LOAD is still unstacking, then the head can never get an AGV.
   - `test_fifo_head_bypasses_full_lane` covers this, and a mutation check confirms the test fails without the exemption.
   - Discharge-only behaviour is unchanged: the existing lookahead test still passes.
3. **AT_TP AGV choice measures distance to the block's ASC position**, because there is no TP position in the port.
4. **SLOT_OCCUPIED / SLOT_EMPTY arriving for an unexpected stage** goes through the generic replan; it cannot loop, because the equipment is excluded until IDLE.
   - **CANCELLED not caused by the watchdog** (external cancel) is also a generic replan.
   - The watchdog's own CANCELLED is ignored because the job was already detached.
5. **Every equipment that enters FAULT is excluded**, even with no running job. IDLE clears it.
6. `status()["pending"]` counts both `AWAIT_AGV` and `AWAIT_ASC`.

## Tests

| file | tests |
|---|---|
| `tests/ecs/test_dispatcher_load.py` | `test_load_happy_path`, `test_load_releases_agv_after_loading`, `test_one_box_per_tp_at_a_time`, `test_stack_out_slot_empty_fails_order`, `test_mixed_fifo_per_sts`, `test_fifo_head_bypasses_full_lane` |
| `tests/ecs/test_dispatcher_recovery.py` | `test_fault_during_transport_replans`, `test_fault_state_change_is_idempotent_with_job_failed`, `test_late_job_failed_does_not_hit_reissued_job`, `test_watchdog_cancels_stuck_agv`, `test_watchdog_counts_movement_as_progress`, `test_watchdog_cancel_event_is_not_double_counted`, `test_box_on_agv_stays_bound`, `test_failed_discharge_is_retried_with_agv_kept_in_lane`, `test_load_rollbacks` (all four load rollbacks, `replans == 4`), `test_fifo_head_requeued_after_agv_fault`, `test_transfer_error_quarantines_agv`, `test_rolled_back_order_keeps_its_position` |

- `test_load_happy_path` has 5 steps and checks both transfer tuples.
- `tests/ecs/fake_port.py`: `FakeSlots` and `make_port` moved here from the discharge test, which now imports them. FakePort's API is unchanged (`set_position` already existed), so it stays backward compatible.

### TDD
**RED.** All new tests were written against the refactored code before implementation:
```
$ uv run pytest tests/ecs -q
FAILED tests/ecs/test_dispatcher_load.py::test_load_happy_path - IndexError: ...
FAILED tests/ecs/test_dispatcher_load.py::test_load_releases_agv_after_loading
FAILED tests/ecs/test_dispatcher_load.py::test_one_box_per_tp_at_a_time - Ass...
FAILED tests/ecs/test_dispatcher_load.py::test_stack_out_slot_empty_fails_order
FAILED tests/ecs/test_dispatcher_load.py::test_mixed_fifo_per_sts - Assertion...
FAILED tests/ecs/test_dispatcher_load.py::test_fifo_head_bypasses_full_lane
FAILED tests/ecs/test_dispatcher_recovery.py::test_fault_during_transport_replans
FAILED tests/ecs/test_dispatcher_recovery.py::test_fault_state_change_is_idempotent_with_job_failed
FAILED tests/ecs/test_dispatcher_recovery.py::test_late_job_failed_does_not_hit_reissued_job
FAILED tests/ecs/test_dispatcher_recovery.py::test_watchdog_cancels_stuck_agv
FAILED tests/ecs/test_dispatcher_recovery.py::test_watchdog_counts_movement_as_progress
FAILED tests/ecs/test_dispatcher_recovery.py::test_watchdog_cancel_event_is_not_double_counted
FAILED tests/ecs/test_dispatcher_recovery.py::test_box_on_agv_stays_bound - a...
FAILED tests/ecs/test_dispatcher_recovery.py::test_failed_discharge_is_retried_with_agv_kept_in_lane
FAILED tests/ecs/test_dispatcher_recovery.py::test_load_rollbacks - Assertion...
FAILED tests/ecs/test_dispatcher_recovery.py::test_fifo_head_requeued_after_agv_fault
FAILED tests/ecs/test_dispatcher_recovery.py::test_transfer_error_quarantines_agv
17 failed, 19 passed in 0.12s
```

**First GREEN run: 3 failed, 33 passed.** All 3 failures were mistakes in the tests, so I fixed the tests:
- The happy path issues 4 jobs over 5 steps, not 5 jobs.
- `test_load_releases_agv_after_loading` needs C2's STACK_OUT completed first.
- In `test_fifo_head_requeued_after_agv_fault`, C3 is legitimately dispatched after C1 starts DISCHARGING, because DISCHARGING leaves the lane.

`test_rolled_back_order_keeps_its_position` was added after a mutation check showed that nothing pinned the requeue position. With `seq` restamped on rollback it fails; with the real code it passes.

**GREEN:**
```
$ uv run pytest tests/ecs -q
37 passed in 0.04s
$ uv run pytest -q
142 passed in 0.64s
$ uvx ruff check packages/ecs tests/ecs
All checks passed!
```

**Mutation checks:** each guard was broken in turn and the expected test failed:

| guard broken | failing test |
|---|---|
| head exemption | `test_fifo_head_bypasses_full_lane` |
| watchdog rollback | `test_watchdog_cancels_stuck_agv` |
| quarantine | `test_transfer_error_quarantines_agv` |
| rollback `seq` | `test_rolled_back_order_keeps_its_position` |
| SLOT_OCCUPIED special case | 3 discharge tests |
| exclusion for bound AGV | `test_box_on_agv_stays_bound` |

## Self-review
- **Completeness:** load path, combined FIFO, TP single-box gate, SLOT_EMPTY, replanning for both paths, FAULT idempotency, exclusion, watchdog, quarantine, and requeue position are all implemented and tested.
- **YAGNI:**
  - No new public API.
  - I removed a defensive check I had added (job→equipment match on events). Job ids are unique, so it was dead code.
  - `_job_eq` stays, because FAULT handling and the watchdog look up running jobs by equipment.
- **Real-behaviour tests:** all tests drive the dispatcher through FakePort events. One test uses a FakePort subclass that emits CANCELLED synchronously, like the sim's `cancel_job`.
- **Output:** pristine, with no warnings in the pytest output (log lines appear only on failure).
- **Architecture:** `grep -r terminal_sim packages/ecs` returns nothing, and `grep -rn "^\s*assert " packages/ecs` returns nothing.

## Concerns
1. **TP capacity across both paths.** The single-box gate only stops a STACK_OUT while a load box occupies or is headed for the TP. A discharge AGV can still deliver to `TP/<b>/WS` while a load box sits there (AT_TP). Whether that is a physical conflict depends on the sim's TP model; Task 15 should check it. A fix would be to hold TO_YARD re-issue/first issue while the block's TP is in `TP_BUSY`, and vice versa.
2. **AGV exhaustion.** Loaded AGVs that wait in a lane behind a head that has no free AGV can still starve the fleet when `lookahead × #STS ≥ #AGVs`. The head exemption fixes the lane-count deadlock, not fleet exhaustion.
3. **Excluded until IDLE.** If an adapter fails a job but never emits `STATE_CHANGED -> IDLE` (the FakePort does not; the sim does), the equipment stays excluded forever. This matches the brief, but the runner should be aware of it.
4. `dispatcher.py` is 415 lines; see the note on `recovery.py` above.

---

## Fix round 1 (review findings)

### What changed

1. **(Important) A head LOAD could be starved of AGVs.** Fixed in `_dispatch_agvs`.
   - New rule: suppose the STS's FIFO head has no `agv_id` and is in `HEAD_NEEDS_AGV_LATER` = {AWAIT_ASC, UNSTACKING, AT_TP}, which is new in `orders.py`. A non-head order of that STS then takes an AGV only if at least 2 AGVs are free, so one stays in reserve for the head.
   - The head itself is still served first, in submit order.
   - The free-AGV list is now one helper, `_free_agvs()`: unreserved and available, meaning not excluded, not quarantined, and IDLE. `_send_agv` uses it too.
   - The reserve is kept per STS. Orders of another STS can still take the last free AGV, so the fleet-exhaustion concern from the main report still applies across STSs.
2. **The crane skipped the lane-AGV check.** Phase 1 now also requires `head.agv_id not in self._excluded | self._quarantined` before it issues DISCHARGE or LOAD.
3. **`status()["bound"]`**: the number of open orders in TO_YARD or TO_STS with no running job whose AGV is excluded or quarantined, i.e. the box is stuck on an AGV that cannot move.

`dispatcher.py` is 434 lines, under the 450 threshold, so it stays one file.

### Tests

| Test | What it checks | Covers |
|---|---|---|
| `test_head_load_is_not_starved_of_agvs` (new) | The reviewer's repro: `make_port(n_agv=2)`, `[LOAD C1, DISCHARGE C2, DISCHARGE C3]`. Only `C2-TRANSPORT-1` is issued at first (AGV-02 is held in reserve). After the ASC and C2's AGV complete, `C1-TRANSPORT-1` goes to AGV-02. Driven to the end: `completed[0] == "C1"`, all three orders complete, `active_jobs == 0`. | Fix 1 |
| `test_crane_does_not_serve_faulted_lane_agv` (new) | C1 is AT_STS while the crane is busy. The lane AGV goes FAULT and the crane frees up: no DISCHARGE is issued. After `IDLE`, `C1-DISCHARGE-1` is issued. | Fix 2 |
| `test_box_on_agv_stays_bound` (extended) | `bound == 1` after the TO_YARD failure, `bound == 0` after IDLE. | Fix 3 |

### RED (before the fix)

```
$ uv run pytest tests/ecs -q
E       KeyError: 'bound'
E       AssertionError: assert [('AGV-01', '...TRANSPORT-1')] == [('AGV-01', 'C2-TRANSPORT-1')]
E       AssertionError: assert ['C1-DISCHARGE-1'] == []
FAILED tests/ecs/test_dispatcher_recovery.py::test_box_on_agv_stays_bound
FAILED tests/ecs/test_dispatcher_recovery.py::test_head_load_is_not_starved_of_agvs
FAILED tests/ecs/test_dispatcher_recovery.py::test_crane_does_not_serve_faulted_lane_agv
3 failed, 36 passed in 0.10s
```

### GREEN

```
$ uv run pytest tests/ecs -q
39 passed in 0.06s
$ uv run pytest -q
144 passed in 0.86s
$ uvx ruff check packages/ecs tests/ecs
All checks passed!
```

`grep -r terminal_sim packages/ecs` returns nothing.
