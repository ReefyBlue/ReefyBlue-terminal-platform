# SDD ledger — plan: docs/superpowers/plans/2026-11-10-plan-b3-events.md
Spec: docs/superpowers/specs/2026-11-10-plan-b3-events-design.md (read; binding authority)
Branch: feat/plan-b3-events (stacked on feat/plan-b1-opcua, PR #2 open)

## Pre-flight scan
| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T1 ↔ T2 | event_fields/event_from_fields(kind, fields) | consistent |
| T2 ↔ T3/T4/T5 | EventEmitter.create(server, types.events, node, eid); EventSink; resolve_event_types; subscribe_terminal_events(client, sub, types); event_from_ua -> (seq, ev) | consistent |
| T2 ↔ T5 | Types.events used by Gateway via register_types | consistent |
| T4 ↔ T5 | T4 test_events_seq_gap_logged pokes mirror._emitter.seq; port rig needs a gateway that emits events | CONFLICT: T4 depends on T5 |
| T1..T5 transitional | removing event_to_json in T1 breaks mirror/port imports until T4/T5 | accepted (plan says so) |
| T1 self | tests vs code | consistent |
| T2 self | test_events + nodeset edits | consistent |
| T3 self | 3 rewritten tests | consistent |
| T4 self | tests reference `_on_event`, `_types` | consistent |
| T5 self | tests reference `_emitter`, `_up_seq` | consistent |
| T6 self | docs + full suite | consistent |

Ruling: execute Task 5 (gateway) before Task 4 (port) — the port's test rig runs a real Gateway and T4's gap test manipulates mirror._emitter; with the plan order T4 would fail until T5 lands — costs nothing if wrong (same code, different order).

Task 1: minor (deferred): codec.event_from_fields — str() coerces None to "None"; float(None) raises TypeError not ValueError; "" vs None round trip lossy (spec sentinel); missing-key test only covers first key. Carried into Task 2: event_from_ua must convert any decode failure (TypeError included) to ValueError.
Task 1: complete (commits 31cab40..8b7c52a, review clean)

Task 2: minor (deferred): concurrency test asserts sorted seqs/sim_times separately (brief said "any order"); Seq gap if trigger() raises (spec: strictly increasing only); EventSink create_subscription(10) logs asyncua "Revised values" WARNING (also port 20 ms / mirror 50 ms); duplicated _free_port/server fixture vs test_nodeset; sinks not detached on assertion failure; kind_of rebuilds dict per call.
Ruling: tests import helpers via sys.path hacks; add "tests/opcua" and "tests/gateway" to pytest `pythonpath` in pyproject (done in Task 3) so `from event_sink import EventSink` / `from tcp_proxy import TcpProxy` work from every test dir — costs nothing if wrong (existing hacks keep working).
Task 2: complete (commits 8b7c52a..78ddfc1, review clean)

Task 3: minor (deferred): test_assign_via_opcua_runs_job waits for 1 event of any kind before asserting JOB_COMPLETED (possible rare flake); received.clear() leaves sink.raw; detach not in finally.
Task 3: complete (commits 78ddfc1..70c9606, review clean)

Ruling: Task 5 — `_up_seq` is NOT reset to 0 on reconnect (my dispatch said so; implementer deviated). Spec §5 requires lost-during-outage events to show as a Seq gap, which a reset would hide. Restarted upstream (Seq back to 1) must still produce no warning: gap only when seq > _up_seq + 1, then _up_seq = seq. Costs if wrong: one spurious warning per upstream restart.

Task 5: minor (deferred): test_event_for_other_equipment_ignored passes if decode fails (add caplog no-"undecodable" assert); before-sim test reads private _emitter.seq; events between read_values and subscribe in _on_connected are lost without gap warning on first connect; tiny post-FAULT window where an in-flight upstream event is re-emitted after the synthesized FAULT (option: drop when _fault_announced); undecodable-event and unknown-State paths untested at mirror level; _up_seq comment should say it is kept across reconnects on purpose.
Task 5: complete (commits 70c9606..d037741, review clean)

Task 4: minor (deferred): test_unknown_event_type_ignored accepts any WARNING (should match message); calls _on_event from test thread (harmless, comment it); _by_node now one-entry dict (simplify); `_types is None` guard unreachable; repeated/backwards Seq delivered without warning (old code warned "not ahead"); docstring could say why decode stays synchronous (asyncua one-task-per-notification); rig reads mirror._emitter.seq.
Task 4: complete (commits d037741..c8e1396, review clean; full default suite 354 passed, 3 deselected)

Task 6: minor (deferred): B1-spec §4 pointer splits the bullet list; README example lacks ValueError catch comment and is 9 lines; README "Verder" line doesn't mark the events step as done; ×100 baseline numbers not captured in report (asserts passed).
Task 6: complete (commits c8e1396..06c3395, review clean; default 354 passed/3 deselected 264 s; e2e_opcua 3 passed 355 s; ×25 moves/h 28.5714 both modes, turnaround 2235 vs 2267 s)

Final review (opus): Important #1 — reconnect window between read_values and subscribe_terminal_events loses an upstream IDLE event → ECS keeps device excluded forever (regression vs B1). Minors to fix before merge: README emitting node line; other-equipment test caplog assert; _up_seq comment; (also tighten unknown-type test message). One fix wave dispatched.

Final fix wave: commits 06c3395..6f600ad; scoped re-review (opus): all 5 addressed, no new breakage. Implementer also put datachange_notification under _write_lock (older snapshot value otherwise overwrote newer State) — accepted. Full default suite 355 passed / 3 deselected.
Ruling: fix option (a) chosen for Important #1 (subscribe before snapshot read; handlers take _write_lock) over (b) re-read State — keeps one ordering mechanism; costs if wrong: a pre-read backlog event may be forwarded before the recovery event (harmless to the ECS per re-review).
