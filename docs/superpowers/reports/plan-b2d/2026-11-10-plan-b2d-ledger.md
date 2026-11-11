# SDD ledger — plan: docs/superpowers/plans/2026-11-10-plan-b2d-link-loss.md
Spec: docs/superpowers/specs/2026-11-10-plan-b2d-link-loss-design.md (read; binding)
Branch: feat/plan-b2d-link-loss (stacked on feat/plan-b2a-ecs-service → PR #4 → #3 → #2)

## Pre-flight scan
| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T1 ↔ T2/T3/T4 | State.OFFLINE, LinkReason values | consistent |
| T1 fixture ↔ T3 | faults_pre_b2d_seed1.json captured before Dispatcher change; T3 asserts equality with auto_release=True | consistent (T3 sets auto_release=True in run_once) |
| T2 ↔ T3 | gateway sends OFFLINE(UPSTREAM_DISCONNECTED) and recovery(<state>, UPSTREAM_RECONNECTED); Dispatcher keys on reason ∈ {LINK_RESTORED, UPSTREAM_RECONNECTED} | consistent |
| T3 ↔ T5/T6 | Dispatcher(auto_release), release/resolve_job/alarms/status keys | consistent |
| T4 ↔ T3 | port emits OFFLINE(LINK_LOST) for all, LINK_RESTORED for all | consistent |
| T4 ↔ T5 | B2a reconcile tests replaced; core tests relying on diff-only LINK_RESTORED adjusted in T4 | consistent |
| T6 ↔ T2 client | EcsClient.alarms/release/resolve_job match app routes | consistent |
| T3/T7 hash | hash test red between T3 and T7 by design | accepted; implementers told |
| T7 self | proxied STS via opcua_procs; existing tcp_proxy | consistent |
| Each task self | tests vs code | consistent |

Task 1: minor (deferred): LinkReason imported inside test fn; exact float compare for moves/h.
Task 1: complete (commits 2317872..e098caf, review clean)

Ruling: tests/opcua/test_port_reconnect_ecs.py now fails (expects FAULT on link loss) — expected transitional red; Task 3 updates it to OFFLINE semantics (no replan, job continues). Costs nothing.
Ruling: mirror placeholder snapshot at start is OFFLINE (implementer went beyond brief; correct: no link yet). Consequence: readiness waits that check "no device FAULT" (runner `_wait_ready`, e2e `_gateway_ready`) must also treat OFFLINE as not-ready — carried into Task 6 (runner) and Task 7 (e2e helper). Costs if wrong: a run could start with devices still OFFLINE.

Task 2: minor (deferred): unknown-state test bypasses _on_connected; unknown state string still written to gateway State var.
Task 2: complete (commits e098caf..9f3f688, review clean)

Ruling: Task 3 extras accepted pending review — crane doesn't serve an AGV that is offline/unreleased; watchdog also frozen for unknown-outcome jobs; STATE_CHANGED for unknown equipment ignored with warning. tests/ecs_service test_reconnect_readmits_device_that_recovered_during_the_outage now red (needs release under B2d) → Task 4/5 rewrite. Costs nothing if wrong.

Task 3: fix round 1/5 (3 addressed, 0 open — FAULT skips unknown-outcome jobs; bound uses _unusable; release-cause detail; commits 74ee360..302f2f2)
Task 3: minor (deferred): resolve-completed test doesn't assert on_transfer/_tp_occupied side effects; release doesn't refuse when still in _offline (safe); tests/kpi builds Dispatcher with default auto_release; _release_cause keeps first cause only.
Task 3: complete (commits 9f3f688..302f2f2, review clean after 1 fix round; baseline + faults equivalence exact)

Task 4: minor (deferred): subscriber exception in unbind loop skips remaining devices and link close; stale snapshot ids after fleet change; RESTORED without LOST after a failed first bind.
Task 4: complete (commits 302f2f2..a4d747e, review clean)

Task 5: fix round 1/5 (2 addressed, 0 open — publish_alarms after drain; blip test asserts progress; commits 4f8cb41..414bcd9)
Task 5: minor (deferred): alarms dict rebuilt every 5 ms poll (cheap; could gate on a dirty flag); alarm message may precede status message of the same step; no test with a buffering fake port for the drain path.
Task 5: complete (commits a4d747e..414bcd9, review clean after 1 fix round)

Ruling: run_once_http gets no FAULT/OFFLINE readiness wait in Task 6 (the service's /health plus the e2e helper's no-FAULT/OFFLINE wait cover tests); runner-side readiness over HTTP is a deferred minor for the final review. Costs if wrong: an http run can post orders while a device is still OFFLINE — the Dispatcher simply won't dispatch to it until LINK_RESTORED.

Task 6: minor (deferred): release/resolve may report 503/false 404 after state already changed (tick raises); no HTTP-level test for release while OFFLINE; double sort in /alarms; e2e helper lacks --auto-release (Task 7).
Task 6: complete (commits 414bcd9..8e5b043, review clean)

Task 7: BLOCKED by product behaviour — at ×100 a 4–5 s outage = 400–500 s sim; a running DISCHARGE completes in the sim, its JOB_COMPLETED is lost; auto_release resolved unknown → failed → re-issued discharge → NO_AGV_IN_LANE (box already on AGV) → endless replan loop; run stalls (7/20).
Ruling: under auto_release an unknown outcome resolves as COMPLETED, not failed. Premise of OFFLINE is that equipment keeps working; a job already running when the link dropped can only end in JOB_COMPLETED or in a FAULT (device returns FAULT, not IDLE); start-time failures (NO_AGV_IN_LANE, SLOT_*) happen before an outage. Manual mode unchanged (operator resolves). Spec §5.1/§7 updated accordingly. Costs if wrong: a job that genuinely failed without a FAULT during an outage is counted done; the sim has no such path, a real vendor adapter must report FAULT.
Ruling: add a replan-streak cap per order (consecutive JOB_FAILED replans without progress ≥ WATCHDOG_MAX_CANCELS → order marked stuck, ERROR log, no further retries) ONLY if the faults-equivalence fixture stays exact; otherwise revert and park. Costs if wrong: an order that would eventually succeed is parked as stuck after 5 immediate failures.

Task 7: unblocked by rulings; commits 976a0d4 (auto_release→completed; replan-streak cap, faults fixture still exact; hash re-baselined) + fb28ab9 (blip e2e, LinkProxy, README, B1 spec). Blips 2/2 pass (20/20, stuck 0, offline back to 0 within 2.3 s). Default 510 passed/6 deselected; e2e_opcua 6 passed.
Task 7: minor (deferred): AGV reserved by a stuck order stays reserved (no operator endpoint to fail/un-park an order); "IDLE after OFFLINE = completed" relies on the equipment-keeps-working premise.
Ruling: on the replan-streak cap the order is FAILED via `_fail_order(rec, "REPLAN_STREAK")` (clears TP and AGV, counted in containers_failed, alarm-free) instead of parked as stuck — a parked order would hold its TP/AGV forever with no operator exit (B2b may add an un-park later). `stuck` keeps its watchdog meaning only. Costs if wrong: an order that might have succeeded on a 6th try is failed; it is logged at ERROR and visible in KPIs.
Task 7: fix round 1/5 (4 addressed, 0 open — streak cap fails order (REPLAN_STREAK), _parked removed, blip tests assert resolve-completed, README; commits fb28ab9..1a8bbb8)
Task 7: minor (deferred): streak-failed order also counted as replan; AGV freed by a failure may still carry a box (pre-existing for YARD_FULL/SLOT_EMPTY — decide quarantine in a later step); rec.stuck not cleared on re-issue; _offline_job may linger (harmless).
Task 7: complete (commits 8e5b043..1a8bbb8, review clean after 1 fix round; default 512 passed/6 deselected; e2e_opcua 6 passed incl. 2 blip tests)

Final review (opus): Important #1 my "unknown = completed" premise is false for the sim (FaultInjector: FAULT + fail_job + MTTR + IDLE can all fall inside a 400–500 s-sim outage) → inflated KPIs / ghost boxes. #2 REPLAN_STREAK frees an AGV still carrying a box. #3 release/resolve report 503/false 404 after state changed. Minors: late outcome leaves needless needs_release; devices OFFLINE at start get no alarm/offline count; spec drift (§5.2 FAULT skips unknown; §7 fleet-mismatch row describes code that doesn't exist); `_excluded ⊇ _needs_release` docstring claim false.
Re-ruling (#1): equipment reports its last job outcome. Model/sim/OPC UA: snapshot extra `last_job_id` + `last_job_outcome` ("COMPLETED"|"FAILED"), set by sim equipment on every completion/failure, exposed as EquipmentType variables `LastJobId`/`LastJobOutcome` (all kinds), mirrored by the gateway as ordinary variables. Dispatcher `_come_back` IDLE-with-job: if `extra.last_job_id == job` → synthesize JOB_COMPLETED/JOB_FAILED accordingly (both modes, no alarm); else truly unknown → manual: operator; auto_release: resolve as FAILED (back to the spec's original, safe default; the sim never hits it). Costs if wrong: a vendor adapter without last-job reporting gets replans + streak cap under auto_release.
Ruling (#2): REPLAN_STREAK (and any `_fail_order`) with the box on the AGV quarantines that AGV (existing mechanism) and adds it to `_needs_release` with cause "quarantined: box may be on board"; `release()` also lifts quarantine. Costs if wrong: an operator release is needed to reuse such an AGV (intended).
Ruling (#3): release/resolve validate membership first, catch ConnectionError from the post-change tick → on_link_lost + success (same pattern as submit).
Ruling (minors): late JOB_COMPLETED/FAILED clears needs_release when "unknown outcome" was its only cause; Dispatcher seeds `_offline` (with LINK_LOST alarm) from snapshots that are OFFLINE at construction; §7 fleet-mismatch row reworded to actual behaviour (no handling; restart the service); spec §5.2 gets the FAULT-skips-unknown line; docstring claim fixed. One fix wave.

## Finish
- Final fix wave: commits 1a8bbb8..888b330 (+ eeb20e2 spec reconcile). Re-review: all 6 findings addressed, no new Critical/Important.
- Ruling retracted: "unknown outcome → completed" (caused silent double-handling when a FaultInjector fault+repair happened inside the outage). Replacement: equipment reports LastJobId/LastJobOutcome; ECS reconciles on reconnect; truly unknown → operator (auto_release → failed); AGV with box on board is quarantined (NEEDS_RELEASE).
- Parked for B2c: assign-during-cut job-id reuse flake (gateway method timeout while upstream executes); quarantine is never lifted under auto_release (documented; unattended runs can stall until operator release); reconciled FAILED always reason FAULT_DURING_OUTAGE.
- Test evidence: default suite 532 passed / 6 deselected; e2e_opcua 6 passed incl. blips (2/2 ×5 runs); equivalence fixtures exact.
