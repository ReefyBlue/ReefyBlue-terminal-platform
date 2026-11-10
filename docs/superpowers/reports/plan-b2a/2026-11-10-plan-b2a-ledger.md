# SDD ledger — plan: docs/superpowers/plans/2026-11-10-plan-b2a-ecs-service.md
Spec: docs/superpowers/specs/2026-11-10-plan-b2a-ecs-service-design.md (read; binding)
Branch: feat/plan-b2a-ecs-service (stacked on feat/plan-b3-events → PR #3 → PR #2)

## Pre-flight scan
| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T1 ↔ T3/T5 | terminal_kpi.kpi.KpiCollector(port, dispatcher, *, faults_total), snapshot(duration_s) | consistent |
| T1 ↔ e2e fixture | snapshot adds sim_elapsed_s; fixture key order | T1 step 4 guards; ruling below |
| T2 ↔ T4 | EcsClient wire format == schemas.WorkOrderIn / AcceptedOut / errors shape | consistent |
| T2 ↔ T3 | SimpleSlotAllocator.occupy(slot) used by EcsCore.submit | consistent |
| T3 ↔ T4 | EcsCore API (connect_once, step, submit, status, kpis, equipment, connected, dispatcher, allocator, broadcaster) | consistent |
| T3 ↔ T5 | EcsCore(layout, connect=lambda: world.port) in equivalence test; connect_once async | consistent |
| T4 ↔ T5/T6 | create_app(core, run_loop=False); serve CLI flags used by e2e helper | consistent |
| T1 self | test asserts snapshot==finish incl. sim_elapsed_s | consistent |
| T2 self | MockTransport tests vs client contract | consistent |
| T3 self | FakePort-based tests; drain_events on FakePort? | FakePort has no drain_events → ruling below |
| T4 self | TestClient WS + AsyncClient REST | consistent |
| T5 self | run_once_http uses _connect for sim control; equivalence drives API directly | consistent |
| T6 self | e2e helper + README | consistent |

Ruling: `KpiCollector.snapshot` sets `sim_elapsed_s` as the LAST key, matching the Plan A fixture order (runner already appended it last); if the fixture order differs, keep the runner-side assignment and let snapshot omit it — Task 1 step 4 decides — costs nothing (key-order test catches it).
Ruling: `ReconnectingPort.drain_events()` exists and delegates to the bound port when it has one (OpcUaEquipmentPort), else is a no-op; `EcsCore.step()` calls `self.port.drain_events()` — so FakePort/SimEquipmentPort (synchronous delivery, no drain) work unchanged — costs nothing if wrong.

User decision (2026-11-10 20:27): link loss is NOT an equipment fault. ECS keeps the running job/assignment (no replan), shows a "connection lost" notification; faults occurring during/after the outage persist until the link is restored AND an operator clears them manually. Planned as separate step B2d (new State OFFLINE, dispatcher change → ECS hash fixture re-baselined, watchdog paused while offline, operator ack/reset endpoint) before the soak test B2c. Out of scope for B2a.

Task 1: minor (deferred): finish lacks docstring saying it aliases snapshot; runner still reassigns sim_elapsed_s (harmless). Implementer saw one unidentified F in a full run that passed on rerun (possible port-clash flake).
Task 1: complete (commits 576c54d..d0007fd, review clean)

Task 2: minor (deferred): submit CLI doesn't catch HTTPStatusError/TransportError (traceback, exit 1 collides with unhealthy code); bare KeyError on missing scenario seed; client 422 parsing assumes {"errors":[...]}; no cross-package parity test submit vs make_workorders (could live in tests/runner).
Task 2: complete (commits d0007fd..c63988b, review clean)

Ruling: Task 3 found `OpcUaEquipmentPort.drain_events()` never raises after link loss, so the service loop could not detect it. Task 4 adds `_check_lost()` at the end of `drain_events()` (after delivering buffered events) in terminal_opcua/port.py plus a test, and the API endpoints catch ConnectionError → `core.on_link_lost()` → 503. Costs if wrong: one extra raise path in the port that the runner's `_advance` already handles (it catches ConnectionError).
Ruling: Task 3 test files renamed (test_reconnecting_port.py, test_broadcaster.py, test_ecs_core.py) to avoid basename clashes with tests/opcua/test_port.py etc. (pytest rootdir import mode) — costs nothing.

Task 3: minor (deferred → carried into Task 4): on_link_lost closes the OPC UA port synchronously (freezes loop up to ~5 s) — move close to to_thread/task; submit() should check `connected` not just dispatcher; run() dies silently on non-ConnectionError — log/supervise. Deferred: reconnect landing exactly on boundary skips that tick; stale buffered events on first bind go to the Dispatcher (runner discards them); _seen_jobs grows unbounded; connect_once while bound overwrites link; spec §6 places ReconnectingPort in core.py but brief in port.py (fine).
Task 3: complete (commits c63988b..dc29adc, review clean; full suite 394 passed / 3 deselected)

Task 4: fix round 1/5 (2 addressed, 0 open — 202 after take on link drop; non-blocking close on bind failure; commits cff7534..c2450bd)
Task 4: minor (deferred): /health 503 body includes sim_time null and new code INVALID_ORDER → update spec §3; port leak if run() cancelled during to_thread(connect) at shutdown; slots leak on a 500 (unreachable); runner's final drain_events outside _advance's try can raise bare ConnectionError; WS idle-disconnect path untested; duplicate tick after Dispatcher.submit; `except BaseException` branch untested. Heads-up Task 5: TestClient is httpx2 — use httpx.ASGITransport for EcsClient in-process.
Task 4: complete (commits dc29adc..c2450bd, review clean after 1 fix round)

Task 5: minor (deferred): sim_elapsed_s in http mode comes from /status (runner t0) while /kpis rates use the service's own elapsed — consider keeping /kpis' sim_elapsed_s (final review decides); service KPI collector counts FAULT events (no faults_total) — belongs with B2d; no readiness wait (no-FAULT fleet) before posting orders in http mode; link loss only caught via 120 s stall although /status shows connected=false; "Start rejected" path untested in http mode; brittle CLI default test.
Task 5: complete (commits c2450bd..9e7a151, review clean; equivalence with Plan A fixture exact)

Task 6: fix round 1/5 (1 addressed, 0 open — public-API SIGTERM handler instead of private uvicorn attribute; commits 686fb10..f42d1ca)
Task 6: minor (deferred): e2e health poll may overrun 60 s by one 2 s timeout; exit code only covered by e2e.
Task 6: complete (commits 9e7a151..f42d1ca, review clean after 1 fix round; default 439 passed/4 deselected; e2e_opcua 4 passed)

Final review (opus): Important #1 reconnect doesn't reconcile state (lost STATE_CHANGED → device excluded forever; lost JOB_COMPLETED → STS stuck); Important #2 sim restart under running ECS unnoticed. Minors to fix now: #3 keep /kpis sim_elapsed_s; #4 README job-message wording; #5 drain_events raise in runner paths; #8 WorkOrderErrorsOut dead → OpenAPI 422 model; #9 spec drift; #10 README note submit CLI (LOAD without prefill fails). Deferred to B2c/B2d: #6 publish_new_jobs O(N); #7 publish_status reads without clients; #11 log noise; #12 allocator never releases; #13 realistic link-drop test variant. One fix wave dispatched.
Ruling: job reconciliation after an outage (lost JOB_COMPLETED) is NOT fixable without touching the frozen ECS → warning + docs now, full fix in B2d (which re-baselines the ECS hash anyway). Costs if wrong: an STS can stall after an outage overlapping a completion until B2d.

Final fix wave: commits f42d1ca..6faeb43; scoped re-review (opus): all 8 findings addressed; no new Critical/Important. Full default suite 451 passed / 4 deselected; e2e_opcua small passed.
Ruling: residual minors B1/B2 (reconcile baseline mixes live reads with delivered events; synthesized events lost if the link drops again mid-reconciliation) are parked for B2d — B2d replaces this mechanism with an explicit OFFLINE state and operator-driven recovery, so a delivered-state map here would be rewritten anyway. Costs if wrong: in a narrow double-fault window a device can stay excluded until B2d lands or the service restarts.
Deferred to B2c: publish_new_jobs O(N) scan; publish_status reads without WS clients; outage log noise every 2 s; allocator never releases slots (YARD_FULL after 3,840 slots in a soak). Deferred to B2d: lost job completions during an outage (WARNING only); faults KPI counts FAULT events; readiness wait in http mode; realistic link-drop test variant (snapshot raising mid-tick).
