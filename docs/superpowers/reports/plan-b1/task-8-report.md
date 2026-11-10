# Task 8 report: runner OPC UA mode, e2e, architecture, README

**Status: DONE_WITH_CONCERNS.** Everything is implemented and committed. The default suite passes. Under `-m e2e_opcua` the small test passes, but `test_baseline_opcua_matches_inprocess` **fails** its timing tolerances (moves/h 2 %, turnaround 60 s). The counts are identical. The cause is structural, not a bug (see Concerns). It needs a controller ruling.

## Commits (branch feat/plan-b1-opcua, on top of 833c7ec)

| Commit | What |
|---|---|
| 86eebae | `test(arch)`: Step 0, `tests/fixtures/ecs_sources.sha256` computed with `git show master:<path>` |
| 5e7b860 | `fix(opcua)`: port hardening (a) asyncio.Lock + `last_seq = max(...)`, (b) `_lost` → `ConnectionError` fast, (c) `close()` gives a fresh queue, + tests |
| 0066a7d | `feat(runner)`: `run_once_opcua`, CLI `--port/--gateway`, KPI faults fallback, marker + addopts, e2e tests |
| 572e84d | `test(arch)`: allow-lists + `test_ecs_unchanged_since_plan_a` |
| 35c8ba1 | `docs`: README section "OPC UA-modus (Plan B1)" |
| 7a916cd | `docs`: README note on the known timing deviation |

All commits carry the requested Sonnet 5.5 trailers.

## Test runs

- **Default suite:** `uv run pytest -q` → `332 passed, 2 deselected in 200.76s`. Shell wall time was 250 s. About 12–16 s of the gap is an exit delay after the summary when the opcua tests run. That delay is pre-existing: I measured it on 86eebae, before the port changes.
- **OPC UA e2e:** `uv run pytest -m e2e_opcua -rA` → wall time 238 s.
  ```
  PASSED tests/e2e/test_opcua_small.py::test_small_scenario_completes_over_opcua
  FAILED tests/e2e/test_opcua_equivalence.py::test_baseline_opcua_matches_inprocess
  E       assert 57.957015213716495 == 60.91370558375635 ± 1.21827
  =========== 1 failed, 1 passed, 332 deselected in 237.59s (0:03:57) ============
  ```
  - The small test: 20/20 done, 0 failed, `timed_out` 0, run took about 37 s wall, both processes exited with returncode 0.
  - The equivalence test fails at the moves/h assert. The counts assert before it passed.

## Equivalence table (baseline seed 1, speed_factor 100; last run)

| KPI | in-process | OPC UA | Δ |
|---|---|---|---|
| `moves_per_hour_total` | 60.91 | 57.96 | −2.96 (−4.9 %) |
| `moves_per_hour_STS-01` | 30.46 | 28.98 | −1.48 |
| `moves_per_hour_STS-02` | 30.46 | 28.98 | −1.48 |
| `agv_wait_sts_s_mean` | 179.3 | 180.3 | +0.92 |
| `agv_wait_tp_s_mean` | 0 | 0 | 0 |
| `agv_travel_s_mean` | 50.39 | 57.98 | +7.60 |
| `utilisation_STS` | 0.9797 | 0.9662 | −0.0135 |
| `utilisation_AGV` | 0.8181 | 0.8333 | +0.0152 |
| `utilisation_ASC` | 0.1396 | 0.1389 | −0.0007 |
| `vessel_turnaround_s` | 11 565 | 12 162 | +597 |
| `faults` | 0 | 0 | 0 |
| `replans` | 0 | 0 | 0 |
| `energy_kwh_per_move` | 8.028 | 8.038 | +0.010 |
| `containers_done` | 200 | 200 | 0 |
| `containers_failed` | 0 | 0 | 0 |
| `containers_stuck` | 0 | 0 | 0 |
| `sim_elapsed_s` | 11 820 | 12 423 | +603 |
| `timed_out` | 0 | 0 | 0 |

Two earlier runs gave moves/h 58.53 and 58.52 (−3.9 %) and turnaround +512 s. So the size of the gap depends on how loaded the machine is.

## Concerns / decisions for the controller

1. **The timing tolerances can't be met with wall-clock pacing (main concern).**
   - **Cost per handoff:** each event → gateway (50 ms subscription) → port (20 ms) → ECS → `AssignJob` round trip costs about 50–100 ms wall. At speed 100 that is 5–10 s sim.
   - **Rounding:** the sim also rounds every handoff up to its 1 s tick, and the STS's 5 s lane poll changes phase.
   - **Total:** the STS is the bottleneck (utilisation 0.98), so the lag adds up over about 100 cycles per STS.
   - **Experiment (not committed):** with gateway 10 ms and port 5 ms / 2 ms poll, the small scenario's turnaround excess only dropped from +153 s to +93 s. I reverted it because the gateway is out of scope.
   - **Rulings that would work:**
     - (a) Widen the tolerances, e.g. moves/h ≤ 7 % and turnaround ≤ 10 %, keeping counts exact.
     - (b) Mark the timing asserts xfail, documented.
     - (c) Defer to a lockstep or step-on-demand clock (sim waits for the runner), which is a spec change.
   - I left the test exactly as specified, so it fails visibly. The default suite does not run it.
2. **PrefillYard is effectively unusable in OPC UA mode.**
   - The sim only accepts it at sim time 0, but `serve` starts advancing immediately, and the runner always connects later.
   - The runner only calls it when there are LOAD orders, and raises a clear `RuntimeError` if it is rejected.
   - The baseline and both e2e scenarios are discharge-only. The small e2e test uses `discharge_share: 1.0` for this reason.
   - Mixed scenarios need a "start on command" in the sim. That is not fixed here and is documented in the README.
3. **Deviations from the controller's spec, and why:**
   - KPI times are relative to `t0`, the sim time when the run starts: `finish(port.now() - t0)`, not `finish(port.now())`. The sim has already run for 15–30 s wall (1 500–3 000 s sim) before the runner connects.
   - `kpi.finish` runs **before** `stop_sim`, because once the servers are stopped the gateway reports them as Fault.
   - Events are drained every 5 ms wall inside each 60 s window, not once per window. The Dispatcher assigns jobs from its event callback, so draining once per window adds up to 60 s of lag per handoff.
   - The timeout rule `max(30, 120*60/speed)` applies per window, as a deadline. A `ConnectionError` from the hardened port is also turned into `RuntimeError("sim time stalled: …")`.
   - The runner waits up to 30 s for the gateway to report no Fault devices before it starts.
   - The test helper waits up to 60 s for the gateway to accept a connection **and** mirror every device as non-Fault.
4. **`agv_travel_s_mean` reads high over OPC UA.** `assigned_at` comes from `port.now()`, which trails the real sim time by the subscription latency. This inflates the value a little.
5. **A failed run leaves the sim running.** If the run raises partway, the runner does not send `Stop`. The test helper SIGTERMs the sim anyway.

## Other notes

- **ECS unchanged:** `git diff --stat master -- packages/ecs` is empty. The hash covers each file as `path NUL content NUL`, in sorted path order, and `test_ecs_hash_detects_a_change` checks that a change is caught.
- **Architecture allow-lists:**
  - `model` → none
  - `opcua` → model (+ `asyncua` and stdlib only)
  - `ecs` → model
  - `sim` → model, opcua
  - `gateway` → model, opcua
  - `tos_stub` → model, ecs
  - `runner` → any
- **New tests:**
  - `tests/opcua/test_opcua_port.py`: `test_connection_lost_fails_fast`, `test_close_discards_buffered_events`, `test_event_seq_handled_in_creation_order_with_direct_read`
  - `tests/runner/test_run_opcua.py`: 3 tests
  - `tests/runner/test_kpi.py`: `test_faults_counted_from_fault_events_without_total`
  - Subprocess helper for the e2e tests: `tests/e2e/opcua_procs.py`

## Files

- `/home/claude/terminal-platform/packages/runner/src/terminal_runner/run.py`
- `/home/claude/terminal-platform/packages/runner/src/terminal_runner/kpi.py`
- `/home/claude/terminal-platform/packages/opcua/src/terminal_opcua/port.py`
- `/home/claude/terminal-platform/tests/e2e/{opcua_procs.py,test_opcua_small.py,test_opcua_equivalence.py}`
- `/home/claude/terminal-platform/tests/runner/test_run_opcua.py`
- `/home/claude/terminal-platform/tests/test_architecture.py`
- `/home/claude/terminal-platform/tests/fixtures/ecs_sources.sha256`
- `/home/claude/terminal-platform/README.md`
- `/home/claude/terminal-platform/pyproject.toml`

---

# Fix round (controller rulings)

**Status: DONE.** Both suites pass. The interrupted implementer's uncommitted diff (Start method end to end, paused SimService, runner order, tests) was correct and complete apart from the README. It is kept as-is and committed in 8e23778.

## Commits

| Commit | Item |
|---|---|
| 8e23778 | `feat(sim)`: paused start. `Start()` → Boolean is on `ControlType`, `create_control_object(start=...)`, `ControlServer(on_start=)`, the gateway `ControlMirror` (forwards it) and `OpcUaEquipmentPort.start_sim()`. `SimService` waits for Start or Stop at SimTime 0 unless `--autostart` is given, and logs "waiting for Start". The runner now runs connect → prefill_yard → submit → tick → `start_sim()` → loop. Tests: `test_sim_starts_paused_until_start` (SimTime stays 0 for 1 s wall, then advances), `test_stop_while_paused_ends_run`, `test_autostart_flag_parsed`, a Start assert in `test_prefill_forwarded` (gateway), `test_start_sim_forwarded` (port), and nodeset asserts. The small e2e test is now `discharge_share 0.5`. README covers the paused start and `--autostart`. (item 2) |
| 8f11ac8 | `test(e2e)`: baseline ×100 tolerance is moves/h 10 % and turnaround 900 s, counts still exact. New test `test_small_scenario_gap_shrinks_with_lower_speed` (×25, moves/h 3 %, turnaround 120 s). The README paragraph on pacing latency also notes step-on-demand as a B2/B3 option. (item 1) |
| 626e599 | `docs`: README, one sentence each for the three accepted deviations. (item 3) |
| 9aeb6eb | `test`: `tests/conftest.py::pytest_sessionfinish` calls `gc.collect(); gc.freeze()`. (item 4) |

## Item 4: exit delay — cause found and fixed

- **Ruled out:** no non-daemon threads were alive at `pytest_unconfigure` (only MainThread), and no asyncua server was left running.
- **Cause:** a `faulthandler` dump showed the time going to `_pytest/unraisableexception.py::gc_collect_harder`, which runs 5 full `gc.collect()` passes. At that point 10–17 M objects were still reachable, nearly all asyncua address-space objects (`AttributeValue`/`DataValue`/`Variant` ≈ 1.8 M each in a 3-test run). Each pass took about 3 s.
- **What keeps them alive:** the `AddressSpace` objects are still referenced via `SimService` → frames/bound methods. I stopped looking for the exact root at the 15-minute budget.
- **Fix:** `gc.freeze()` at session finish makes those passes cheap. Measured on `tests/sim/test_serve.py`: before, 72 s reported vs 97–110 s wall; after, 75.1 s reported vs 75.5 s wall.
- **Trade-off:** unraisable exceptions from objects that would only be collected at exit are no longer reported.

## Test runs

Default suite (`uv run pytest -q`, wall 236 s):
```
336 passed, 3 deselected in 234.83s (0:03:54)
```
OPC UA e2e (`uv run pytest -m e2e_opcua -q -rA`, wall 368 s):
```
PASSED tests/e2e/test_opcua_equivalence.py::test_baseline_opcua_matches_inprocess
PASSED tests/e2e/test_opcua_equivalence.py::test_small_scenario_gap_shrinks_with_lower_speed
PASSED tests/e2e/test_opcua_small.py::test_small_scenario_completes_over_opcua
3 passed, 336 deselected in 368.59s (0:06:08)
```
The small mixed test (10 LOAD orders via PrefillYard) finished 20/20 done, 0 failed, 0 timed out.

## Equivalence (in-process vs OPC UA, paused start)

| KPI | baseline ×100 in-proc | ×100 OPC UA | Δ | small ×25 in-proc | ×25 OPC UA | Δ |
|---|---|---|---|---|---|---|
| `moves_per_hour_total` | 60.91 | 58.52 | −2.39 (−3.9 %) | 28.57 | 28.57 | 0 |
| `agv_wait_sts_s_mean` | 179.3 | 182.1 | +2.7 | 147.2 | 148.2 | +1.0 |
| `agv_travel_s_mean` | 50.39 | 53.65 | +3.3 | 45.42 | 45.94 | +0.5 |
| `utilisation_STS` | 0.980 | 0.978 | −0.002 | 0.857 | 0.857 | 0 |
| `utilisation_AGV` | 0.818 | 0.824 | +0.006 | 0.929 | 0.952 | +0.024 |
| `utilisation_ASC` | 0.140 | 0.111 | −0.029 | 0.036 | 0.054 | +0.018 |
| `vessel_turnaround_s` | 11 565 | 12 088 | +523 | 2 235 | 2 265 | +30 |
| `energy_kwh_per_move` | 8.028 | 8.020 | −0.008 | 8.016 | 8.016 | 0 |
| `containers_done` / `failed` / `stuck` | 200/0/0 | 200/0/0 | 0 | 20/0/0 | 20/0/0 | 0 |
| `faults` / `replans` | 0/0 | 0/0 | 0 | 0/0 | 0/0 | 0 |
| `sim_elapsed_s` | 11 820 | 12 303 | +483 | 2 520 | 2 520 | 0 |
| `timed_out` | 0 | 0 | 0 | 0 | 0 | 0 |

(×25 is the discharge-only small scenario: 20 containers, 1 STS, 2 AGV, seed 7.)

## Concerns

- The e2e_opcua suite now takes about 6 min instead of the expected 5, because the ×25 test adds about 2 min.
- With `--autostart` the sim cannot run a LOAD scenario. It fails with a clear `RuntimeError`, and this is documented.
- `packages/ecs` is unchanged (the hash test passes), and the architecture allow-lists still pass.
