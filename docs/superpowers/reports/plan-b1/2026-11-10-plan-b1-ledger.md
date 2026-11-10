# SDD ledger — plan: docs/superpowers/plans/2026-11-10-plan-b1-opcua.md

Spec: docs/superpowers/specs/2026-11-10-plan-b1-opcua-design.md (read). Branch feat/plan-b1-opcua from master a20d46c.

## Pre-flight conflict scan
| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T1 codec ↔ T2/T4/T6/T7 | job/event JSON functions | consistent |
| T2 nodeset ↔ T4 EquipmentServer, T6 Mirror | register_types, create_equipment_object, write_snapshot, emit_event | consistent |
| T2 emit_event order (LastEvent then EventSeq) ↔ T7 client reads LastEvent on EventSeq change | T4 buffers multiple events per sync and emits each separately; T6 mirror must forward LastEvent before EventSeq | consistent; mirror ordering is a T6 requirement (noted in brief) |
| T3 Handover subscribes first on SimEquipmentPort ↔ KpiCollector/Dispatcher subscription order | World creates Handover in __init__ before runner subscribes | consistent |
| T3 removes KpiCollector.observe_transfer ↔ tests/runner/test_kpi.py | brief says adapt tests | ok |
| T5 ControlServer PrefillYard ↔ T6 gateway forward ↔ T7 port.prefill_yard ↔ T8 runner | json list of {slot, container_id}; T7 signature list[tuple[str,str]] → port encodes | consistent |
| T5 SIM_TICK_S=1.0 ↔ T8 equivalence tolerance (2 %, 60 s) | reaction latency ≤ 1 s sim | plausible |
| T6 terminal.yaml `opcua:` block ↔ load_layout (T3 Plan A) | load_layout must tolerate/parse new key | T6 adds Layout.opcua fields — Ruling 1 |
| T7 port thread ↔ dispatcher single-threaded | drain_events only | consistent |
| T8 ecs hash fixture | computed on master; T3 must not touch packages/ecs | consistent (T3 touches sim+runner only) |
| env | asyncua 2.1.0 installable | ok |

Ruling 1: `Layout` gets optional fields `opcua_base_port: int = 48400` and `opcua_host: str = "localhost"` read from a top-level `opcua:` block in terminal.yaml (defaults when absent) — the plan mentions the yaml key but not the dataclass; cost if wrong: none.

## Progress
Task 1: minor (deferred): event bad-enum path untested; architecture guard not yet extended for opcua (planned in T8); no type validation of job fields from JSON; codec duplication
Task 1: complete (commits 6c7c66b..1b48b58, review clean)
Task 2: Ruling: all OPC UA method wrappers are async coroutines so handlers run on the event-loop thread (asyncua runs sync methods in a thread pool) — prevents data races with the sim/runner loop — cost if wrong: none.
Task 2: minor (deferred): _VAR_CACHE/_EMITTERS weak-key dicts never collected (values hold session); EventEmitter counter starts at 0 regardless of node value; Variant raises on None for Double/Boolean extra values; some nodeset paths (STS/ASC objects, block_folder, CancelJob) untested. NOTE for T4–T7: method handlers run on loop thread; datachange only fires on value change.
Task 2: fix round 1/5 (3 addressed, 0 open; commits d2637e6..4c11fe4)
Task 2: complete (commits 1b48b58..4c11fe4, review clean)
Task 3: Ruling (superseding my dispatch instruction): a discharge AGV arriving at a TP that already holds a box off an AGV keeps its box and waits; unloaded when that TP's STACK_IN completes; tp_box entries tagged by origin so a LOAD box may share — restores spec §3; my FIFO instruction rested on a false premise (ECS blocks a second TO_YARD leg only after a box has arrived) — cost if wrong: none; matches Plan A hook behaviour exactly on 5 scenarios.
Task 3: fix round 1/5 (1 addressed; commit bc6a5b5..2b44b8a); re-review dispatched.
Task 3: minor (deferred): YARD_FULL-failed order leaves a ghost discharge box in sim tp_box → next discharge AGV at that TP is stranded for good (ECS unaware); not reachable in shipped scenarios (yard never fills) — follow-up: log error / drop ghost when ECS issues STACK_IN for another box; `_release_waiting_agv` ignores AGV FAULT state; redundant _sync in ASC branch.
Task 3: complete (commits 4c11fe4..2b44b8a, review clean after 1 fix round)
Task 4: minor (deferred): events lost if sync() fails midway (restore buffer on exception); non-ValueError handler exceptions surface as UaStatusCodeError not False; double start() leaks a server; sync() before start uses assert. NOTE for T7: subscribe with default queue size and small sampling interval to avoid coalescing.
Task 4: complete (commits 2b44b8a..de9b8d7, review clean)
Task 5: Ruling: ControlType gains SimTime/SpeedFactor variables (spec listed them on ControlType but T2 omitted them) — cost: none.
Task 5: minor (deferred): startup ~14 s for 13 asyncua servers (standard address space load, CPU-bound; sharing/caching non-trivial — note for B2/B3); no catch-up cap after a long stall; SIGTERM during startup waits for startup; pacing test wall-clock based; PrefillYard rollback branch untested. NOTE for T8: idle tick ≈ 4–5 ms wall → speed_factor ceiling ≈ 200 idle; use ≤ 100 for e2e; allow ≥ 45 s for server startup.
Task 5: fix round 1/5 (3 addressed, 0 open; commits 4d5610a..b70987b)
Task 5: complete (commits de9b8d7..b70987b, review clean)
Task 6: Ruling: EquipmentHandlers may be coroutine functions; nodeset wrappers await awaitable results (gateway forwards async) — cost: none. Ruling: Gateway(layout, n_sts=2, n_agv=6) derives the upstream port table; discovery is B2 — cost: must match the sim's fleet.
Task 6: Ruling: gateway never writes LastEvent without bumping EventSeq; on resync delivers the upstream's latest event only if seq and payload are new, at max(self._seq+1, upstream_seq); live path skips already-delivered pairs — a missed JOB_COMPLETED or a shifted pairing blocks the ECS, a duplicate does not — cost if wrong: duplicate events after reconnect.
Task 6: fix round 1/5 (3 Important + 2 minor addressed; commits 0e21a72..74ab4d1); re-review dispatched. Full suite 304 passed in 176 s (suite time growing; T8 to gate heavy tests).
Task 6: fix round 2/5 (1 Important + 1 minor addressed; commits 74ab4d1..412b199)
Task 6: minor (deferred): two byte-identical consecutive events shift FIFO pairing (OPC UA sends no notification for an unchanged LastEvent) — practically unreachable (needs same kind, job id and sim_time) but note for B3 (structured events fix it); spurious WARNING on fresh upstream with EventSeq 0; 2 s method timeout may return False for a job the upstream did accept; no STATE_CHANGED emitted on recovery (snapshot is leading); test helper ordering nit.
Task 6: complete (commits b70987b..412b199, review clean after 2 fix rounds)
Task 7: dispatched (opus) 13:32Z
Task 7: minor (deferred → carried into T8 dispatch): fallback direct-read in _on_event_seq can reorder events and move last_seq backwards (wrap handler in asyncio.Lock; last_seq = max); lost connection not surfaced to callers (set _lost flag → fail fast so runner exits 1); close() doesn't reset _events; FIFO has no self-check for a stray LastEvent; untested prefill_yard/stop_sim/timeout paths.
Task 7: complete (commits 412b199..833c7ec, review clean)
Task 8: Ruling: default pytest run excludes `e2e_opcua` (addopts -m 'not e2e_opcua'); explicit `uv run pytest -m e2e_opcua` documented — suite already ~190 s — cost: OPC UA e2e not run by default.
Task 8: Ruling: over OPC UA `faults` KPI = count of STATE_CHANGED→FAULT events (World.faults_total unavailable) — cost: counts gateway-synthesized Fault events too (documented).
Task 8: Ruling: opcua mode rejects --replications > 1 in B1 (needs a fresh sim per replication; B2 orchestration).
Task 8: dispatched (opus) $(date -u +%H:%M)Z
Task 8: fix round 1 (tolerance 10%/900s + ×25 gap test; paused start with Start()/--autostart; README deviations; exit-delay check) — first implementer interrupted by user with 11 uncommitted files; fresh opus implementer resumed from the partial diff at 14:35Z per user 'optie 1'.
Task 8: fix round 1 complete (commits 8e23778..9aeb6eb): paused start + Start(); tolerances 10%/900s at ×100 (measured −3.9%/+523 s), ×25 exact moves/h; README; gc.freeze fix for pytest exit delay. Default suite 336 passed 235 s; e2e_opcua 3 passed 369 s. Task review dispatched on full range 833c7ec..9aeb6eb.
Task 8: Ruling (recording drift): small e2e runs at speed_factor 100 instead of the spec's 200 — the idle tick ceiling is ~200 and the gateway adds load (T5 note) — cost: slower e2e, no semantic impact.
Task 8: minor (deferred): e2e readiness helper compares "Fault" not "FAULT" (ineffective, compensated by runner _wait_ready); runner failure path never sends Stop (sim stays paused/running — README note or best-effort stop_sim); README says 5 ms drain (≥10 ms real) and ≈3 min suite (235 s); ×25 rate band 3 % thin vs 60 s windows (widen to 5 % or use turnaround); fresh queue in close() created before disconnect; sim process orphaned if gateway spawn raises in helper.
Task 8: complete (commits 833c7ec..9aeb6eb, review clean after 1 fix round)
ALL B1 TASKS COMPLETE — final whole-branch review next.
FINAL REVIEW (opus): With fixes. Important: I1 no STATE_CHANGED on gateway recovery → ECS keeps device excluded forever; I2 failed runner never sends Stop; I3 gateway fleet mismatch silently shifts upstream ports. Minors: fallback pairing misattribution risk (seq-in-payload for B3); port/gateway timeouts equal (2 s); SimTime vs events over separate connections (second timing effect); README staleness; e2e "Fault" typo; no fault scenario over OPC UA; ×25 band thin; bind 0.0.0.0 vs spec localhost.
Ruling: I1 fix = gateway synthesizes STATE_CHANGED(reason=UPSTREAM_RECONNECTED, new_state=upstream state) on resync after an announced Fault — spec success criterion 3 must hold end to end; cost: an extra event the ECS treats as recovery.
Ruling: I2 = best-effort stop_sim in finally; I3 = runner asserts fleet counts vs scenario and exits 1 naming --n-sts/--n-agv.
Parked — Ruling: comm loss treated as equipment FAULT by the ECS (BUSY device's job replanned while the sim finishes it) — needs ECS or gateway-side CancelJob logic that the spec forbids in B1; decision for B2/B3.
Parked — Ruling: bind address stays 0.0.0.0 (needed for Docker/NAS in B2); security is B3.
Parked — Ruling: no fault scenario e2e over OPC UA in B1 (timing-dependent replans; add at ×25 with count tolerances in B2 duration test).
FINAL FIX WAVE dispatched (opus) $(date -u +%H:%M)Z.
FINAL RE-REVIEW (opus): all findings addressed, no new Critical/Important breakage.
Parked — Ruling: no recovery event when the upstream itself is FAULT on reconnect (device stays excluded until its own IDLE); BUSY recovery does not lift exclusion — consistent with ECS semantics.
PLAN B1 COMPLETE: 29 commits a20d46c..11a243d; default suite 344 passed (~4 min); e2e_opcua 3 passed (~6 min).
