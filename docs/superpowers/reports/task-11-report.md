# Task 11 report — ECS: werkorders en lossen

**Status:** DONE_WITH_CONCERNS (dispatcher.py is 302 lines, just past the ~300 guideline; split point below)
**Commit:** `e3d0a6d feat(ecs): work orders, FakePort and discharge dispatch` (branch feat/core-plan-a)

## Implemented
- `packages/ecs/src/terminal_ecs/workorders.py`: `WorkOrder` (frozen), `SlotProvider` Protocol, `WorkOrderError(ValueError)` with `.code`.
- `packages/ecs/src/terminal_ecs/dispatcher.py`: `Dispatcher` (discharge path only), `Stage` enum, internal `_OrderRecord`.
- `packages/ecs/src/terminal_ecs/__init__.py`: re-exports `Dispatcher, SlotProvider, WorkOrder, WorkOrderError`.
- `tests/ecs/fake_port.py`: `FakePort` as specified (`calls`, `calls_cancel`, `calls_reset`, `complete`, `fail`, `set_state`, `advance`, plus a small `set_position` helper). Events are sent to subscribers synchronously.
- `tests/ecs/test_dispatcher_discharge.py`: 15 tests.

## Tests
The five tests from the brief, plus:
- duplicate against an already-submitted container
- INVALID_SLOT, parametrized over: unparseable, a TP rather than a slot, a block with no ASC
- exact `on_transfer` tuples
- YARD_FULL → failed
- a rejected `assign_job` falls back to the next AGV
- STACK_IN waits for a busy ASC (FIFO)
- the equipment map in `status()`
- re-entrancy, using an `EchoPort` that emits STATE_CHANGED inside `assign_job`. A mutation check confirmed it: disabling the guard makes this test fail.

**RED** (before implementation):
```
E   ModuleNotFoundError: No module named 'terminal_ecs.dispatcher'
ERROR tests/ecs/test_dispatcher_discharge.py
1 error in 0.08s
```
First GREEN attempt showed 1 failure. The test was wrong, not the code: the happy path issues 4 jobs, not 5. After fixing the test: `14 passed`. After adding the re-entrancy test: `15 passed in 0.03s`.
**Full suite:** `120 passed in 0.61s`. `uvx ruff check packages/ecs tests/ecs`: All checks passed. `grep -r terminal_sim packages/ecs` returns nothing.

## State machine (for Task 12)
Each order is an `_OrderRecord(order, yard_slot, stage, agv_id, current_job_id, seq, fail_reason, job_counts)`.
**Rule: `current_job_id is None` means "this stage still needs its next assignment".** `tick()` issues all assignments by scanning for such records. `on_event` only moves records between stages and never calls `assign_job` itself.

```
AWAIT_AGV(None)    --TRANSPORT-1 (agv->sts)-->  AGV_EN_ROUTE(job)   --done--> AT_STS(None)
AT_STS(None)       --DISCHARGE-1 (VESSEL->sts)--> DISCHARGING(job)  --done, on_transfer sts_to_agv--> TO_YARD(None)
TO_YARD(None)      --TRANSPORT-2 (sts->TP/b/WS)--> TO_YARD(job)     --done, on_transfer agv_to_tp, agv freed--> STACKING(None)
STACKING(None)     --STACK_IN-n (TP->slot)-->    STACKING(job)      --done--> DONE (removed from _open, cid appended to completed)
STACKING(job) JOB_FAILED SLOT_OCCUPIED --> next_slot(block, exclude=old): new slot → STACKING(None), replans+=1
                                                                    None → FAILED, fail_reason YARD_FULL, failed+=1
```
- `_issue(eid, rec, job, stage)` does the book-keeping (stage, seq, `jobs`, `_by_job`) **before** `port.assign_job`, because the port may emit events synchronously. It rolls back if the assignment is rejected. Job-id suffixes come from `rec.job_counts[type]` and increase only when an assignment is accepted, so a rejected try reuses the same id.
- `seq` is restamped on every stage entry and gives the FIFO order for waiting records: AT_STS by arrival at the STS, STACKING by arrival at the TP.
- `tick()` phases, in order:
  1. DISCHARGE for each idle STS
  2. TRANSPORT-2
  3. STACK_IN for each idle ASC
  4. send AGVs to STSes, limited by the lookahead

  In-flight work therefore takes priority over new dispatch.
- An AGV counts as reserved while its record has `agv_id` set, i.e. from EN_ROUTE through TO_YARD. This covers the AT_STS case, where the port reports the AGV as IDLE but it is standing in the crane lane.
- Re-entrancy is handled with `_in_tick` / `_dirty` flags: a nested `tick()` only sets the dirty flag, and the outer `tick()` loops again.
- Collections: `_open` (cid → record; insertion order is the per-STS submit FIFO), `_known` (all cids, for duplicate checks), `_by_job` (job_id → record), and the public `jobs` / `completed`.

## Decisions / deviations to note
- **Lookahead counts only AGV_EN_ROUTE + AT_STS**, as the brief says. Once an order enters DISCHARGING, its AGV in the lane stops counting, so the next AGV is sent right away. Under the crane this can mean up to lookahead+1 AGVs per STS. The lookahead test asserts exactly this behaviour (C3 is dispatched after C1 starts discharging). If that is not intended, add DISCHARGING to `_LOOKAHEAD_STAGES`.
- The AT_STS → DISCHARGE order is by AGV arrival, not submit order. Arrival order avoids the STS waiting idle for an earlier order's AGV.
- LOAD orders are validated and enqueued (they count in `pending`) but are skipped by the AGV dispatch; they do not block the FIFO. Any type other than DISCHARGE/LOAD raises a plain `ValueError`, because the brief defines no code for it.
- Positions are read live via `snapshot()` at dispatch time, not cached at init. Only the kind classification and ASC-per-block mapping are done in `__init__`.
- Non-SLOT_OCCUPIED JOB_FAILED events are logged and the order is left as it is: the job stays in `jobs` and the stage is unchanged. This is Task 12's hook. `watchdog_s` is stored but not used yet.
- `status()["equipment"]` values are `State` (StrEnum), so they compare equal to `"IDLE"` etc.

## Self-review
Completeness: everything in the brief and decisions is covered. YAGNI: no load path and no fault handling. Tests drive real behaviour through `FakePort` events. Test output is clean (no warnings from the project). No `terminal_sim` imports.

## Concerns
- dispatcher.py is 302 lines. **Suggested split for Task 12:** move `Stage`, `_OrderRecord` and the transition logic (`_on_completed` / `_on_failed` stage changes) into `terminal_ecs/orders.py`. Keep `Dispatcher` for port I/O: `_issue`, `tick` phases, AGV selection. The load path then adds stages and tick phases without growing the dispatcher.
- See the lookahead note above.

---

## Fix round 1 (review findings)

### Changes (`packages/ecs/src/terminal_ecs/dispatcher.py`)
1. **(Important) Per-STS FIFO discharge.** A new `_fifo_head(sts)` returns the earliest DISCHARGE order for that STS, in submit order, whose stage is still AWAIT_AGV, AGV_EN_ROUTE or AT_STS (`_PRE_DISCHARGE`). Tick phase 1 issues DISCHARGE only when that head is AT_STS, has no job yet, and the STS is IDLE. An AGV that arrives early for a later order waits in the lane. This replaces the arrival-order rule; the earlier note that "AT_STS → DISCHARGE is by AGV arrival" no longer applies.
2. **Slot from `next_slot` is validated.** New `_valid_slot(slot, block)`: the slot must parse, be a `SlotLoc` and be in the order's block. If not, the dispatcher logs an error and treats it as YARD_FULL.
3. **Transfer hook is hardened.** `_enter(...)` now runs before the hook. `_transfer(rec, kind, eid)` wraps the hook in try/except, logs with `log.exception` (ERROR level with exc_info; ruff G201 prefers this over `error(..., exc_info=True)`), and fails the order with `TRANSFER_ERROR`. The new `_fail_order(rec, reason)` is shared with YARD_FULL: it sets fail_reason, clears agv_id, sets stage FAILED, removes the record from `_open` and does `failed += 1`. Clearing agv_id releases the AGV reservation. Physically, the AGV may still hold the box; Task 12 can decide what recovery means here.
4. **STACK_IN and asserts.** After any STACK_IN attempt the ASC is added to `busy_asc`, so each ASC gets at most one STACK_IN per pass. The TO_YARD assert in tick is now a log-and-skip. The assert in `_on_completed` TO_YARD is gone: it uses `rec.agv_id or ev.equipment_id`.

The module docstring is updated for the new transitions. dispatcher.py is now 334 lines; it stays one file per the controller's decision.

### New tests (`tests/ecs/test_dispatcher_discharge.py`)
- `test_discharge_follows_per_sts_fifo_not_arrival_order`: C2's AGV arrives first and no DISCHARGE is issued. When C1's AGV arrives, `C1-DISCHARGE-1` is issued. `C2-DISCHARGE-1` is issued only after STS-01 completes.
- `test_invalid_slot_from_provider_is_treated_as_yard_full`: the provider returns `B/1/1/1` for a block-A order. Result: no STACK_IN-2, failed=1, replans=0.
- `test_raising_transfer_hook_fails_order_without_stranding_it`: the hook raises on `sts_to_agv`. Result: C1 FAILED (failed=1), no TRANSPORT-2 for C1, and the dispatcher still discharges C2 afterwards.
- `test_stack_in_assigns_at_most_one_job_per_asc_per_tick`: a port whose ASC keeps reporting IDLE after accepting a job, with two boxes waiting at TP/A/WS. Only `C1-STACK_IN-1` is issued.

### TDD
RED, before the fixes (`uv run pytest tests/ecs -q`): `4 failed, 15 passed`. Failure reasons:
```
E  AssertionError: assert not [Job(job_id='C2-DISCHARGE-1', ...)]          # FIFO
E  AssertionError: assert 5 == 4  (STACK_IN to 'TP/B/WS' -> 'B/1/1/1')    # slot validation
E  RuntimeError: world says no                                             # hook
E  ['C1-STACK_IN-1', 'C2-STACK_IN-1'] == ['C1-STACK_IN-1']                 # busy_asc
```
GREEN:
```
$ uv run pytest tests/ecs -q
19 passed in 0.03s
$ uv run pytest -q
124 passed in 0.61s
$ uvx ruff check packages/ecs tests/ecs
All checks passed!
```
`grep -r terminal_sim packages/ecs` returns nothing, and no `assert` statements remain in `packages/ecs`.
