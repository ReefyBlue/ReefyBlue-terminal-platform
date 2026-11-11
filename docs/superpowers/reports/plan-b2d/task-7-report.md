# Task 7 report: e2e blips, ECS hash, docs, full suite

**Status: BLOCKED.** Both blip tests fail deterministically because of how the product behaves (see the analysis below). No product code was changed and **nothing is committed**. The working tree holds every other deliverable, ready for the controller to decide.

## Changes in the working tree (uncommitted, on top of 8e5b043)

- `tests/e2e/opcua_procs.py`
  - New `LinkProxy(routes: dict[listen, target])`. It is a threaded, sync TCP proxy. `start()`, `stop()`, `cut(ports=None)` and `restore(ports=None)` work per route. A cut sends RST on every open connection of the route and refuses new ones (accept, then RST) until restore. It is the threaded twin of `tests/gateway/tcp_proxy.py`, which is asyncio-based and does not fit a test whose main thread blocks in `run_once_http`.
  - `sim_and_gateway(..., proxied_sts=False)`: when True, the gateway gets `--base-port P` and the proxy maps `P+i -> B+i` (sim base B) for all 13 slots, control slot included. `info["proxy"]` and `info["sts_port"]` (= P, the STS-01 slot) are exposed. Only that route is cut in test (a), so only the gateway↔STS-01 link drops. This follows the brief's "proxy between gateway and one STS server" without having to split the sim's contiguous port range.
  - `ecs_service(..., auto_release=False)` adds `--auto-release`.
- `tests/e2e/test_blip_during_run.py` (new, `e2e_opcua`). Two tests: (a) the sim↔gateway STS-01 route; (b) a one-route proxy between the ECS service and the gateway (the runner's own gateway link stays direct). Each test:
  - runs SMALL (reused from `test_service_small`) with the service on `--auto-release`;
  - runs `run_once_http` in a thread;
  - cuts the link 10 s after the run starts and restores it 3 s later;
  - polls `GET /status` every 0.2 s while the link is cut.

  The test asserts done == 20, failed == 0, stuck == 0, timed_out == 0, that `offline` was above 0 while cut, `offline == 0` at the end, and a wall time under 120 s.
- `tests/fixtures/ecs_sources.sha256` re-baselined to `37945385e9a10b048d0799b2e070205e991215c00db688b6a529f01903be52ce`. It was computed with the test's own function: `uv run python -c "import sys; sys.path.insert(0,'tests'); from pathlib import Path; from test_architecture import _ecs_sources_sha256; print(_ecs_sources_sha256(Path('.').resolve()))"`. **If the fix for the blocker touches `packages/ecs`, run this again.**
- `README.md`
  - New section "Storingen, verbindingsverlies en vrijgave (Plan B2d)". It covers the IDLE/BUSY/FAULT/OFFLINE table, the alarm kinds, the three endpoints with curl examples and their status codes, the 409 rules, what the operator does after a FAULT and after an unknown outcome, `--auto-release`, and `--no-auto-release` (which has no effect with `--port http`).
  - The old B2a paragraph on link loss is shortened. It described "LINK_RESTORED only on difference", which B2d removed, and now points to the new section.
- `docs/superpowers/specs/2026-11-10-plan-b1-opcua-design.md` §4: one added line, "OFFLINE: zie Plan B2d-spec …".

## Suite results

- `uv run pytest -q`: **508 passed, 6 deselected in 258 s**. This includes `tests/test_architecture.py` with the new hash.
- `uv run pytest -m e2e_opcua -q`, existing tests only: **4 passed in 389 s**, so the `opcua_procs` changes do not break them.
- `tests/e2e/test_blip_during_run.py`: **2 failed**, identically in two separate runs. Each test takes about 75 s of wall time, because the run only ends at `duration_s`.

## Blip results: the test infrastructure works

- (a) The gateway logged `STS-01: connection … lost` at 10.0 s wall. The ECS logged `STS-01 offline (UPSTREAM_DISCONNECTED); job C0008-DISCHARGE-1 kept`, then `STS-01 back online: IDLE` 5.3 s later (3 s cut plus the gateway's 5 s `RECONNECT_S` retry).
- (b) `equipment link lost at sim time 978.0`, and all 7 devices went OFFLINE (LINK_LOST). `/status` kept answering from cache, with one 503 right at the cut. The link reconnected after 4.0 s (3 s cut plus the 2 s `CONNECT_RETRY_S`), and every device came back with LINK_RESTORED.

## Failure: product behaviour

KPIs in both cases: `containers_done == 7` of 20, and the run ended on the `duration_s` limit.

ECS log for (a); (b) is the same, plus a second unknown outcome, `C0007-TRANSPORT-2` on AGV-02:

```
STS-01 offline (UPSTREAM_DISCONNECTED); job C0008-DISCHARGE-1 kept
STS-01 back online: IDLE (job C0008-DISCHARGE-1)
outcome of C0008-DISCHARGE-1 on STS-01 unknown after link loss
resolve C0008-DISCHARGE-1 on STS-01: failed            <- auto_release (spec §5.1 / §7)
replan C0008: DISCHARGING failed on STS-01 (UNKNOWN_OUTCOME) -> AT_STS
replan C0008: DISCHARGING failed on STS-01 (NO_AGV_IN_LANE) -> AT_STS   x46 (a) / x90 (b), until the run ends
```

What happens:

1. At speed 100, an outage of 4–5 s of wall time is 400–500 s of sim time. The discharge that was running **finishes in the sim during the outage**: the STS cycle completes, `Handover.try_discharge` puts C0008 on the lane AGV, and the STS becomes IDLE. Its `JOB_COMPLETED` is lost with the link.
2. The STS comes back IDLE with an open job. By spec, `auto_release=True` then resolves the job directly as **`failed`**, and C0008 is replanned to AT_STS.
3. The ECS sends the discharge again. In the sim the box has already left the crane, and no empty AGV with `job_container_id == C0008` is in the lane. The STS fails the job with `NO_AGV_IN_LANE` (`sts.py:63`) and the ECS replans it the same way, endlessly. STS-01 is the only crane in SMALL, so the whole quay side stops and the run times out at 7 of 20.

Whenever an outage overlaps a running job, a job that actually completed gets treated as "failed". Retrying a DISCHARGE (and probably a TRANSPORT) that already happened in the world never succeeds. So the spec's assertion that the run completes without `stuck` cannot hold for these scenarios. The failure is deterministic, not a flaky test: at speed 100, any outage longer than one STS cycle hits it.

Options for the controller (each one changes the product or the spec):

- **A. Reconcile the job from evidence on return to IDLE.** Examples: a DISCHARGE whose container is now `LoadedContainerId` of an AGV counts as completed; a TRANSPORT whose AGV is at its destination TP counts as completed. Use this with auto-release instead of a blind "failed", or use it in general.
- **B. Under auto-release, resolve unknown outcomes as `completed`.** In the sim the device only returns IDLE without a `JOB_FAILED` when the job finished. This is risky as a general rule and needs a spec change to §5.1/§7.
- **C. Cap the replan retry loop.** For example, mark the order failed after N identical `NO_AGV_IN_LANE` failures. The run would end, but with `containers_failed > 0`, so the test still fails as specified.
- **D. Change the spec's test expectation for blips.** For example, allow failed > 0 but require no stuck orders and no time-out. Without option C the run still times out.

Trimmed logs (terminal_*, uvicorn and WARNING/ERROR lines only) are in `.superpowers/sdd/2026-11-10-plan-b2d-link-loss/task-7-logs/{a,b}-{ecs,gateway,sim}.log`.

## Other notes

- With the proxy, the gateway's first upstream attempts log `BrokenPipeError` instead of "connection refused", because the proxy accepts and then resets while the sim is not up yet. It retries and connects normally.
- Planned commit once unblocked (from the brief): `test/docs: link blips during OPC UA runs; ECS hash re-baselined for B2d; README on faults, link loss and release`, with the given trailer.

---

# Follow-up after the controller ruling (DONE)

Commits: `976a0d4` (product + ECS tests + hash), `fb28ab9` (e2e blip tests + docs). Both carry the given trailer.

## Product changes
1. `Dispatcher._come_back`: under `auto_release=True` an unknown outcome now resolves as **completed**. The docstring gives the reason: during OFFLINE the equipment keeps working, so a job that was running can only end in JOB_COMPLETED or FAULT, and start-time failures happen before the outage. Manual mode is unchanged. The module docstring, the `serve --auto-release` help text and the `make_dispatcher` docstring in `fake_port` are updated to match. The test is now `test_auto_release_resolves_unknown_as_completed`: replans == 0, no alarms, no re-issued transport, and the order advanced (the STS gets the discharge).
2. Replan-streak cap. `OrderRecord.replan_streak` is incremented by `_replan(..., streak=True)`, which is called from `_on_failed`'s generic branch and from the FAULT replan. It is reset in `_on_completed` together with `watchdog_cancels`/`stuck`. When it reaches `WATCHDOG_MAX_CANCELS` (5), the order gets `stuck = True` and an ERROR `replan streak: <cid> failed <n> times in a row on <eid> (<reason>); parked as stuck` is logged.
   - Before this, `stuck` only applied to watchdog orders that still had a running job, and dispatch never read it. A new helper `_parked(rec)` (stuck and no running job) is now skipped in `_waiting`, `_fifo_head` and `_dispatch_agvs`, so a parked order is never re-issued and the next order becomes the STS head. Watchdog-stuck orders keep their job, so `_parked` is False for them and their behaviour is unchanged.
   - New tests: `test_replan_streak_parks_order_as_stuck` (5 immediate NO_AGV_IN_LANE failures give stuck == 1, no 6th assign, and the ERROR text is logged) and `test_replan_streak_resets_on_completion`.
   - **Constraint met:** `test_plan_a_equivalence` and `test_faults_equivalence` pass EXACT, so change 2 is kept.
3. ECS hash re-baselined again to `9e28ebcb06007031a9e54fbacb36499598db7c948964c350c6c104bfa9387e36`, using the same procedure.

## Test fix (infrastructure only)
The first re-run passed (b) but failed (a) on the final `offline == 0` check, even though that run itself completed 20/20. The logs show the cause: after the run, the runner sends `Stop`, the sim stops serving, and the gateway reports every device OFFLINE. That races with the `/status` call made after the run. The test now polls `/status` after the restore, while the run is still going, until `offline == 0`, with a limit of `RECOVER_S = 15` s.

## Results
- Blip tests: **2 passed** (100 s for both).
  - (a): cut at 10.0 s, done 20, failed 0, stuck 0, timed_out 0, run 31.7 s wall, max offline while cut 1, offline back to 0 2.3 s after the restore.
  - (b): done 20 / failed 0 / stuck 0 / timed_out 0, run 33.4 s, max offline 7, back to 0 1.5 s after the restore.
  - In both, `replans` == 0, and C0008-DISCHARGE-1 resolved as completed.
- `uv run pytest tests/ecs tests/e2e/test_plan_a_equivalence.py tests/e2e/test_faults_equivalence.py tests/test_architecture.py -q`: 105 passed.
- `uv run pytest -q`: **510 passed, 6 deselected** (275 s).
- `uv run pytest -m e2e_opcua -q`: **6 passed** (508 s), i.e. the 4 existing tests plus the 2 new ones.

## Docs
- README section "Storingen, verbindingsverlies en vrijgave (Plan B2d)" now also says that under `--auto-release` an unknown outcome counts as completed (without it the operator decides via `resolve`), and describes the replan-streak cap (`stuck`).
- B1 spec §4 has the OFFLINE line.

## Concerns
- A parked order keeps its `agv_id`, if it has one. An AGV bound to a parked order stays reserved until the run ends. That is acceptable for "parked", but an operator endpoint to un-park or fail such an order does not exist yet.
- Change 1 trusts that "IDLE after OFFLINE with no JOB_FAILED" means the job completed. That holds for the sim. For real equipment it rests on the premise in the spec.

---

# Fix round 1 (review of 976a0d4..fb28ab9)

Commits: 4998601, 1a8bbb8 (code+tests, docs).

- **IMPORTANT 1 + 2 (ruling):** the `_parked` predicate is gone, along with its three dispatch-time checks (`_waiting`, `_fifo_head`, `_dispatch_agvs`). `stuck` is back to its watchdog-only meaning. When the streak reaches `WATCHDOG_MAX_CANCELS` (5), `_replan` now logs ERROR `replan streak: <cid> failed <n> times in a row on <eid> (<reason>); order failed` and calls `_fail_order(rec, "REPLAN_STREAK")`, which frees the TP and the AGV and counts the order in `failed`. Streak counting is unchanged (JOB_FAILED and FAULT replans count; watchdog and resolve-failed neither count nor reset) and is documented in the dispatcher docstring, the `OrderRecord` comment, the README and the new spec §5.3a "Herhaalde mislukkingen".
- **Tests:**
  - `test_replan_streak_fails_order`: failed == 1, open == 0, stuck == 0, no 6th assign, the ERROR text is logged, and a new order gets the freed AGV.
  - `test_replan_streak_in_stacking_frees_the_tp`: ASC fails STACK_IN 5 times, and `_tp_occupied` is empty afterwards.
  - `test_watchdog_stuck_order_is_reissued_after_its_job_fails`.
  - All 3 fail against the code of fb28ab9 (checked) and pass now.
- **MINOR 6:** both blip tests assert that the ECS log contains `resolve <job> on <eid>: completed` and no `: failed`. The cut always hits C0008-DISCHARGE-1 (seed 7, cut at 10 s), and in (b) also C0007-TRANSPORT-2. This held in all 4 runs observed, so I assert it in both tests.
- **MINOR 7:** the README sentence on REPLAN_STREAK says what the operator sees: `failed` / `containers_failed` and the ERROR line.
- **Hash:** `bd8c203783cca3947cc24622c165ce8aae42a3960b1f5e89ecc76e779966348b`.
- **Results:**
  - `tests/ecs` + Plan A / faults equivalence + architecture: 107 passed; both equivalence tests still match exactly.
  - Blip tests: 2 passed (102 s). (a) done 20, failed 0, stuck 0, timed_out 0, offline back to 0 2.8 s after the restore. (b) the same, back to 0 after 1.2 s.
  - Default suite: 512 passed, 6 deselected (280 s).
- **Concern:** a REPLAN_STREAK failure while the box is on an AGV (e.g. TO_YARD) frees that AGV even though it may still carry the box. Unlike TRANSFER_ERROR, it does not quarantine the AGV. This follows the ruling, but worth a look.

## Re-review of fix round fb28ab9..1a8bbb8

Verdicts: Important 1 ADDRESSED (dispatcher.py `_waiting`/`_fifo_head`/`_dispatch_agvs` no longer reference `_parked`; `_parked` deleted; `stuck` set only in `_watchdog`; test `test_watchdog_stuck_order_is_reissued_after_its_job_fails`). Important 2 ADDRESSED (dispatcher.py `_replan` streak cap -> `_fail_order(rec, "REPLAN_STREAK")` + ERROR log; docstring, README, spec §5.3a; tests `test_replan_streak_fails_order`, `test_replan_streak_in_stacking_frees_the_tp`). Minor 6 ADDRESSED (`_assert_resolved_completed` in both blip tests). Minor 7 ADDRESSED (README paragraph).

Consistency check: `_replan` calls `_detach` before the cap check, so `_job_eq`/`_by_job`/`jobs` are already cleared when `_fail_order` runs; no dangling job. `_unknown` is untouched but cannot hold the job: JOB_FAILED clears it via `_outcome_seen`, the FAULT loop skips unknown jobs, and `resolve failed` does not use streak. AGV-with-box: `_fail_order` sets agv_id None without quarantine, identical to YARD_FULL/SLOT_EMPTY; only TRANSFER_ERROR quarantines. Pre-existing, not new.
