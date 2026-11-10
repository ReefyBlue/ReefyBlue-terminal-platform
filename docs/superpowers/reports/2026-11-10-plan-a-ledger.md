# SDD ledger — plan: docs/superpowers/plans/2026-11-10-terminal-platform-core.md

Spec: docs/superpowers/specs/2026-11-10-terminal-platform-fase1-design.md (read)
Branch: feat/core-plan-a (from master 7bf76bb)

## Pre-flight conflict scan

| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T1 types ↔ T6/T8/T11 | Job.from_loc/to_loc, JobType | consistent |
| T2 port ↔ T9/T11/T14 | EquipmentPort 7 methods incl. now() | consistent |
| T5 Grid ↔ T7 AGV | try_reserve/release/owner_of/center/shortest_path | consistent |
| T5 Grid ↔ T6 STS | STS polls `owner_of(lane_cells[0])`; T9 resolve_cell sends AGV to "first free lane cell" → may be lane_cells[1] | CONFLICT → Ruling 1 |
| T7 AGV ↔ T9 World | resolve_cell(loc) -> Cell | consistent |
| T8 ASC ↔ T4 Yard ↔ T13 allocator ↔ T15 prefill | occupy/release; allocator keeps own books | consistent |
| T11 ↔ T12 ↔ T15 | on_transfer kinds sts_to_agv/agv_to_tp/tp_to_agv/agv_to_sts | consistent |
| T11 ↔ T13 | SlotProvider.next_slot(block, exclude) | consistent |
| T14 KPI (sim pkg) imports Dispatcher (ecs) | allowed; constraint is only ecs→sim | ok |
| T9 test | TP/A/WS cell (2,5) center = (50,110) with 20 m cells | consistent |
| T6 | rng.triangular(90,150,110) = (low, high, mode) | ok |
| T16 | plausibility 20–45 moves/h per STS vs 90–150 s cycle | plausible; plan allows adjusting with justification |
| env | Python 3.13 available; requires-python >=3.12 | ok |

Ruling 1: STS considers an AGV "in lane" when EITHER of its two lane cells is owned by an AGV (owner id starts with "AGV-"); `handover_lane_occupied` reflects the same — the plan text (lane_cells[0] only) would deadlock when World routes the AGV to the second cell — cost if wrong: slightly looser handover semantics, no spec impact.

## Progress
Task 1: minor (deferred): JsonFormatter silently drops extra keys named ts/level/logger/msg; test_logging doesn't restore root level; locations accept leading zeros (A/01/1/1); equipment regex broader than closed ID set; tests/* lacks __init__.py (basename collision risk)
Task 1: fix round 1/5 (1 addressed, 0 open — workspace deps via root dev group; commits 0862ce9..7990d8c)
Task 1: complete (commits d53ab80..7990d8c, review clean)
Task 2: minor (deferred): protocol member count not pinned by test; EquipmentPort lacks contract docstrings (False on rejection, KeyError unknown id, sync callbacks); extra Mapping mutability note
Task 2: complete (commits 7990d8c..9bb8721, review clean)
Task 3: minor (deferred): unknown fault keys silently ignored (typo → no faults); fault values not range-checked; missing yaml keys raise KeyError not ValueError; no layout geometry validation
Task 3: complete (commits 9bb8721..6a92d5b, review clean)
Task 4: minor (deferred): tier stacking not enforced (tier 3 occupiable above empty tiers); test loads terminal.yaml relative to cwd; duplicate block names silently overwrite in Yard ctor
Task 4: fix round 1/5 (3 addressed, 0 open — slot range check, CONTAINER_PLACED guard, is_free KeyError; commits 574bf65..328ad6f)
Task 4: complete (commits 6a92d5b..328ad6f, review clean)
Task 5: minor (deferred): owner_of skips out-of-grid check; unreachable test contains an unrelated reachability assertion; Grid ctor doesn't validate sizes; Cell alias duplicated in layout.py and grid.py
Task 5: complete (commits 328ad6f..b654d04, review clean)
Task 6: minor (deferred): reset() while a job runs leaves process/current_job alive (plan-mandated literal); late-AGV test has no upper bound; event-order sequence not asserted; cancel() branch of fail_job not directly asserted
Task 6: fix round 1/5 (2 addressed, 0 open — fail_job cancels process; yieldless guard; commits 62b187b..70a6d59). Controller verified salabim exposes env._yieldless.
Task 6: NOTE for later tasks: Environment must be created with yieldless=False (greenlet not installed); passive component via process=""; activate(process="process") per job; Equipment.accepts(job) hook.
Task 6: complete (commits b654d04..70a6d59, review clean)
Task 7: minor (deferred): contention test invariant weak (<=4 cells, no retry assertion); untested fail_job-mid-route / cancel-in-wait-loop / loaded speed / exact battery values; battery unclamped; route not trimmed as AGV advances; orphan component on rejected start cell. NOTE: a stationary AGV blocks its cell (no deadlock avoidance beyond watchdog).
Task 7: complete (commits 70a6d59..94fe4fd, review clean)
Task 8: Ruling: ASC cycle model = leg(current→pick)+hoist(pick)+leg(pick→place)+hoist(place), leg=|Δbay|·6.5/4+|Δrow|·3/1.5, hoist=2·tier·3/1 — the plan's "≈52.5 s" example was inconsistent with its own constants; A/10/3/1 from bay 1 = 33.875 s — cost if wrong: ASC throughput in KPIs differs from the plan author's mental number, no spec impact.
Task 8: minor (deferred): no interruption test; extra() untested; STACK_OUT timing not asserted; TP occupied flags never set (wire in World/Plan B); trolley row not tracked between jobs
Task 8: complete (commits 94fe4fd..7bf3d06, review clean)
Task 9: Ruling: AGV routing plans around cells reserved by others (Grid.shortest_path avoid=, Grid.reserved_by_others) with unconstrained fallback, and re-plans after REPLAN_AFTER_S=30 s waiting on a non-goal cell (goal cell: keep waiting) — plan's static route deadlocked with the baseline fleet (AGV-01 blocked by parked AGV-02 on its only tie-broken first step) — cost if wrong: slightly longer routes while others move; no spec impact.
Task 9: minor (deferred): transfer_agv_to_tp uses assert for type check; AGV pick in transfer_sts_to_agv follows fleet order; duplicated STS lookup; goal cell not re-resolved on re-plan (STS lane may be taken while other lane free — dispatcher-level, carry to Task 11); coverage gaps (reserved-goal branch, cancel during wait)
Task 9: fix round 1/5 (ruling implemented, 0 open; commits 3a866e1..495b2e8)
Task 9: complete (commits 7bf3d06..495b2e8, review clean)
Task 10: Ruling: fault path sets FAULT before failing the job; Equipment.fail_job skips the IDLE transition when state is FAULT (no transient IDLE that a synchronous subscriber could dispatch into) — plan's order (fail_job then FAULT) was unsafe — cost if wrong: none expected; event sequence differs from plan text.
Task 10: minor (deferred): snapshot-state assertion in test_fault_fails_running_job is seed/timing dependent (make event-based if flaky); World ignores negative mtbf when bypassing load_scenario
Task 10: fix round 1/5 (3 addressed, 0 open; commits dd0cba1..c29ec7d)
Task 10: complete (commits 495b2e8..c29ec7d, review clean)
Task 11: Ruling: lookahead counts only AGV_EN_ROUTE and AT_STS (an AGV under the crane is not "en route"), so up to lookahead+1 AGVs may be around a crane — matches plan text; cost if wrong: lane congestion, tunable.
Task 11: Ruling: DISCHARGE strictly follows per-STS submit FIFO (early-arriving AGV waits in lane) — spec §5 says FIFO per STS — cost if wrong: slightly lower crane utilisation.
Task 11: minor (deferred): _valid_slot unparsable/non-SlotLoc branches untested and TypeError not caught; log lines put ids in msg not extra=; fail_reason not retrievable after order leaves _open; dispatcher.py 334 lines (split orders.py in Task 12). CARRY TO TASK 12: FIFO-head stall when head's AGV faults/fails must requeue or fail the head; TRANSFER_ERROR releases AGV while box may still be on it.
Task 11: fix round 1/5 (4 addressed, 0 open; commits e3d0a6d..d979d07)
Task 11: complete (commits c29ec7d..d979d07, review clean)
Task 12: implementer done (ea0c6bc refactor orders.py, e5658aa load/replan/watchdog); review in progress. Env restarted once; state verified (142 passed).
Task 12: Ruling: re-issued jobs after a fault use an incremented suffix (C1-TRANSPORT-2), never reuse an id — the sim emits STATE_CHANGED FAULT before JOB_FAILED and a reused id would be matched to the new job — brief said "nieuwe TRANSPORT"; cost: none.
Task 12: Ruling: one combined per-STS submit FIFO for DISCHARGE and LOAD; FIFO head may exceed lookahead; while a head LOAD still needs an AGV, non-head orders of that STS leave one AGV free — prevents the deadlock the reviewer reproduced — cost if wrong: lower AGV utilisation in mixed sequences.
Task 12: Ruling: TRANSFER_ERROR quarantines the AGV (never dispatched again) instead of releasing it with the box possibly on board — cost: fleet shrinks on a hook bug; visible in status.
Task 12: minor (deferred): reserve rule is fleet-wide not per STS (multi-STS reserve untested); watchdog on a bound AGV (TO_YARD/TO_STS) and on AGV_TO_TP untested; watchdog may cancel healthy TRANSPORTs queued behind an STS outage > watchdog_s (replans inflate, no deadlock); excluded-until-IDLE ends immediately for non-FAULT failures because sim emits JOB_FAILED then IDLE; quarantined AGV never released; a discharge AGV may deliver to a TP holding a load box (CARRY TO TASK 15: check sim TP capacity model). dispatcher.py 434 lines.
Task 12: fix round 1/5 (3 addressed, 0 open; commits e5658aa..5d1f886)
Task 12: complete (commits d979d07..5d1f886, review clean)
Task 13: minor (deferred): allocator duplicates Yard fill-order loop (guard test covers only first 50 slots of A); generator lacks ValueError for layout/n_sts mismatch; release() splits strings instead of parse_location; disjoint-slot assertion tautological
Task 13: complete (commits 5d1f886..29e7edc, review clean)
Task 14: minor (deferred): job-type parse assumes container ids without '-' (use rsplit); energy/replans/failed only tested at 0; tests/sim/conftest.py sys.path hack for fake_port (move to tests/support or pytest pythonpath); kpis dict untyped
Task 14: complete (commits 29e7edc..4de6b51, review clean)
Task 15: implementer dispatched (opus) at 00:56Z
Task 15: implementer DONE_WITH_CONCERNS (6 commits 360b929..0afa57a; 20-container run completes; 200-container scenarios time out at 196-198 because freed AGVs park on handover cells; lookahead 3 AGVs on a 2-cell lane; package cycle sim→ecs/tos_stub→sim; INFO log noise). Review dispatched.
Task 15: Ruling (pending implementation): freed AGVs must clear handover cells — sim-side auto-clear: an AGV that is IDLE on an STS lane cell or TP cell for > AUTO_CLEAR_AFTER_S = 5 s moves itself (state BUSY, no job events) to the nearest free staging cell in grid rows 2–3, then IDLE — mirrors real AGV "clear handover position" rules; cost if wrong: extra empty moves in KPIs.
Task 15: Ruling (pending): lookahead counts all orders whose AGV is bound to the crane area — discharge {AGV_EN_ROUTE, AT_STS, DISCHARGING}, load {TO_STS, AT_STS_LOAD, LOADING} — so lookahead 2 matches the 2-cell lane; supersedes the Task 11 ruling — cost if wrong: slightly lower crane utilisation.
Task 15: Ruling (pending): break the package cycle — move kpi.py and run.py into a new workspace package `terminal_runner` (depends on sim, ecs, tos_stub); sim depends on model only; tos_stub on model, sim, ecs. CLI becomes `python -m terminal_runner.run`. Task 16 README/tests follow.
Task 15: Ruling (pending): per-assignment dispatcher logs at DEBUG; INFO reserved for replans, failures, run summaries.
Task 15: Ruling (R1 amended): only an EMPTY AGV with no pending handover auto-clears (AGVs waiting under the crane must stay) — cost if wrong: none.
Task 15: Ruling (R2 amended): load lookahead stages include AGV_TO_TP (AGV sent to TP is committed to that crane) — mixed sweep 80/80 completes — cost if wrong: lower AGV utilisation on load-heavy sequences.
Task 15: fix round 2/5 (R1–R4 + 4 small items; commits e0e847a,7c3c5a2,83810a7,9671bcc,eb3fceb); fix round 3/5 (AGV_TO_TP lookahead + docs; 5982ccf,2b00a4a). Scenarios: baseline 200/200 60.9 mph 11820 s; agv4 200/200 60.6 mph; faults 200/200 40.0 mph 18000 s. Re-review dispatched.
Task 15: minor (deferred): FAULT during an auto-clear move doesn't stop the clearing generator (faulted AGV may still take one step; AGV may briefly hold two cells if fault starts+ends within a step) — fix: in AGV.set_state on FAULT with no job cancel the clear, release pending, clear route; run_replications(replications=0) called directly still IndexErrors; job_container_id clear on fail path untested; parked AGVs on rows 2–3 could obstruct routing in larger fleets.
Task 15: re-review clean (rounds 2–3). 
Task 15: complete (commits 4de6b51..2b00a4a, review clean)
Task 16: minor (deferred): architecture guard passes vacuously if a package dir is missing/empty; regex misses exotic import forms; self-test covers only `from` form
Task 16: complete (commits 2b00a4a..d31cf21, review clean)
ALL TASKS COMPLETE — final whole-branch review dispatched.
FINAL REVIEW (opus): With fixes. Critical: C1 runner stops early when orders are parked behind faulted equipment (faults 10 reps: done mean 198.8 yet timed_out 0); C2 load/mixed+faults lane deadlock via head exemption (21/40 mixed, 27/45 load-only hit cap). Important: I3 AGV wait/utilisation KPIs miss committed-idle lane time (misleading for fleet studies); I4 TP holds unlimited boxes (ASC outages understated); I5 speed_factor/share_40ft unused but documented; I6 tos_stub depends on sim (spec: model only shared); I7 ghost reservation on short fault during auto-clear. Minors listed in report.
FINAL FIX WAVE dispatched (opus, single implementer) for C1, C2, I3–I7 + minors at $(date -u +%H:%M)Z.
Ruling: C2 fix = always keep one lane slot free for the FIFO head while its AGV is not yet in the lane; watchdog escalates to `stuck` after 5 cancels of one order (no silent loop) — cost if wrong: slightly lower lane utilisation.
Ruling: I3 = event-based AGV KPIs (agv_wait_sts_s_mean, agv_wait_tp_s_mean, agv_travel_s_mean = transport duration) and utilisation_AGV counts committed-idle as utilised — replaces plan's sample-based definition — cost if wrong: KPI semantics differ from plan text; documented in README.
Ruling: I4 = TP capacity 1 modelled in the ECS (tp_occupied per block, AWAIT_TP stage allowed) with re-slot to another block while the ASC is faulted — spec §4 did not model TP capacity; cost: lower discharge throughput under ASC faults (more realistic).
Ruling: I6 = tos_stub takes plain arguments (block dims, sts ids) and prefill moves to runner so tos_stub depends only on model+ecs — spec §13 "model is the only shared dependency"; cost: API churn in tos_stub.
FINAL FIX WAVE done: 8 commits 23a8575..1d66174; 254 passed 45 s; 300-run sweep complete; residuals: LOAD+discharge box may share TP; loaded AGV waiting for TP not in lookahead; re-slot leaves old allocator slot used. Re-review dispatched.
FINAL RE-REVIEW (opus): all findings addressed, no new Critical/Important breakage. Determinism confirmed under PYTHONHASHSEED 1/99; 36-run 3× fault-rate stress sweep all 200/200.
Parked — Ruling: a LOAD box and a discharge box may share a TP in Plan A — the symmetric one-box rule deadlocked immediately; proper fix (unstack only once an AGV is committed, or multi-position TPs) is Plan B — cost: realism under ASC faults, not liveness.
Parked — Ruling: a loaded AGV waiting in the lane for its TP is not counted against lookahead — no wait cycle (depends on ASC, not lane); Plan B list.
Parked — Ruling: re-slot leaves the old allocator slot marked used (same as SLOT_OCCUPIED path) — tiny volume; SlotProvider.release is a Plan B API change.
Parked — Ruling: sim `tp_ws_occupied` only true from STACK_IN assignment, not from agv_to_tp — nothing in Plan A reads it; set it in World.transfer_agv_to_tp in Plan B before OPC UA exposure.
PLAN A COMPLETE: 48 commits 7bf76bb..1d66174, 254 tests passing.
