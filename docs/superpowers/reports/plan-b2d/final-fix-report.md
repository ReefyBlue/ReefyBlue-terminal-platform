# B2d final-review fix wave — report

Branch feat/plan-b2d-link-loss, base 1a8bbb8. Commits: ac1f611 (sim/opcua), 0c0eb50 (ecs + hash), bbfaf89 (ecs_service), 888b330 (docs).

## Item 1 — last job outcome reported by equipment
- Sim: `packages/sim/src/terminal_sim/equipment/base.py:50-51` `last_job_id`/`last_job_outcome`; `_record_outcome` (≈:112) called in `cancel_job` (:72, FAILED), `complete_job` (:95), `fail_job` (FAILED), before the event is emitted; `snapshot()` adds both keys to `extra` (subclass `extra()` untouched). AGV overrides call super, so covered.
- OPC UA: `packages/opcua/src/terminal_opcua/nodeset.py:26` `LastJobId`, `LastJobOutcome` in `EQUIPMENT_VARS` (all kinds), `EXTRA_KEY_MAP` :48-49, `VAR_TYPES` :57 (String), written explicitly in `write_snapshot` :281-282 (`""` for None). Gateway `mirror.py:171` derives `var_names` from `EQUIPMENT_VARS` → mirrored automatically; ECS port reads `Extra` JSON → passes keys through unchanged.
- FakePort (`tests/ecs/fake_port.py`): `set_last_job()`, `finish_silently()`; `complete()`/`fail()` set them; snapshot extra carries both keys.
- Dispatcher `_come_back` (dispatcher.py:635): IDLE with job → `extra.last_job_id == job` → INFO `reconciled %s on %s from equipment: %s`, `_outcome_seen` + `_on_completed` / `_on_failed(reason FAULT_DURING_OUTAGE)` (both modes, no alarm); else unknown → manual `_unknown` + needs_release; auto_release → `_resolve(job, "failed")`. Module docstring, README, spec §2/§5.1/§7 updated (§5.3a kept).
- Tests: `tests/ecs/test_dispatcher_offline.py` — reconciled COMPLETED/FAILED (parametrized both modes), unrelated last job manual → unknown, `test_auto_release_resolves_as_failed_when_truly_unknown`; `tests/sim/test_sts.py` (+5), `tests/sim/test_agv.py` (cancel); `tests/opcua/test_nodeset.py::test_last_job_variables_exist_and_are_written`.
- Blip e2e: `_assert_reconciled` requires the `reconciled …` line and no `resolve … : failed`. Logs show e.g. `reconciled C0008-DISCHARGE-1 on STS-01 from equipment: COMPLETED`.

## Item 2 — quarantine AGV carrying a box
- `orders.py:64` `BOX_ON_AGV = {TO_YARD, AWAIT_TP, TO_STS, AT_STS_LOAD, LOADING}` (sts_to_agv/tp_to_agv until agv_to_tp/agv_to_sts; failed LOADING rolls back to AT_STS_LOAD with box on board).
- `_fail_order` (dispatcher.py:790) quarantines `rec.agv_id` in those stages via `_quarantine` (:715: `_quarantined`, needs_release with cause "quarantined: box may be on board" (overrides earlier cause), ERROR log). `_transfer` TRANSFER_ERROR now uses the same helper (so it also raises NEEDS_RELEASE and is releasable — previously "for good"). `_apply_state` IDLE with auto_release does not clear release for quarantined devices (:612). `release()` lifts quarantine after the same checks.
- Tests (`tests/ecs/test_dispatcher_recovery.py`): streak in TO_YARD → quarantined, alarm detail, not dispatched even after IDLE w/ auto_release, release → dispatched; TRANSFER_ERROR raises NEEDS_RELEASE and release re-admits.
- Faults/Plan A fixtures stayed exact (no delta).

## Item 3 — API atomicity
- `Dispatcher.has_equipment`, `unknown_jobs` (:241/:244); `release(..., tick=True)` / `resolve_job(..., tick=True)` keyword.
- `core.py:245/262`: pre-check membership (KeyError only there; internal KeyError → RuntimeError), call dispatcher with `tick=False`, `_tick_after_command` (:286) catches ConnectionError → `on_link_lost()` and returns normally; resolve also treats ConnectionError inside the verdict as post-change. App docstring updated; routes unchanged (404 now only from pre-check).
- Tests `tests/ecs_service/test_app.py`: release 200 when tick loses link + health 503 + retry 503; release retry after reconnect 200 / 409 before change / 404 unknown; resolve 200 when tick loses link, retry 503 then 404 after reconnect; internal KeyError is not a 404.

## Item 4 — late outcome clears needs_release
- `_outcome_seen` (:680): pops `_unknown`; clears release if cause is "unknown outcome" and no FAULT since (`_faulted` set, cleared on release), not quarantined, no other unknown job on the device. Tests: late completion clears; FAULT in between keeps it.

## Item 5 — OFFLINE at construction
- `__init__` (:178) `_go_offline(eid, "OFFLINE_AT_START")` for snapshots reporting OFFLINE (alarm since = now). Test `test_offline_at_construction_is_seeded`.

## Item 6 — docs
- Spec §5.2 FAULT-skips-unknown line, quarantine, OFFLINE-at-start, late outcome; §7 fleet row reworded; new §7 rows. Dispatcher docstring: "State sets" paragraph states the real relation (needs_release not ⊆ excluded). README Onbemande runs, alarms table, operator actions, quarantine.

## Verification
- `uv run pytest tests/ecs tests/sim tests/opcua tests/gateway tests/ecs_service tests/e2e/test_plan_a_equivalence.py tests/e2e/test_faults_equivalence.py -q` → 345 passed (later 349 incl. extra ecs_service tests in full run).
- ECS hash regenerated (df8166b5…e2009); `tests/test_architecture.py` → 29 passed.
- `tests/e2e/test_service_small.py -m e2e_opcua` → 1 passed; `tests/e2e/test_blip_during_run.py -m e2e_opcua` → 2 passed ×5 consecutive + ECS-blip ×4 (all 20/20).
- Full `uv run pytest -q` → 532 passed, 6 deselected.
- ruff: 57 pre-existing errors on base, none added.

## Concerns
- Blip e2e flakes seen in 2 of ~12 file runs early on (not reproduced in the later 9 runs):
  (a) ECS-blip run ended 19/20 with `replan streak: C0006 failed 5 times … (CONTAINER_PLACED)` — the cut hit an `assign_job` (STACK_IN-1 "rejected" by ASC-B, i.e. the call's reply was lost) but the sim executed it; every re-issue then fails CONTAINER_PLACED. Pre-existing assign-during-cut ambiguity, independent of this wave (assign path unchanged); reconciliation cannot help because the dispatcher had already detached the job. Needs a later decision (e.g. treat assign ConnectionError as "unknown assignment" and reconcile via LastJobId/CurrentJobId).
  (b) sim process return code -15 at teardown once (sim killed by the harness timeout) — looks like teardown timing; not investigated further.
- `FAULT_DURING_OUTAGE` is used for every FAILED last-job report, so a SLOT_OCCUPIED/SLOT_EMPTY failure during an outage takes the generic replan path (no reslot / no fail). Equipment could also report a last-job reason if needed.
- TRANSFER_ERROR quarantine is now operator-releasable (was permanent) and raises NEEDS_RELEASE — follows the "any `_fail_order`" ruling.
- Commit trailer: used the caller's `Claude Fable 5.1` line (repo convention); the session's system attribution says `Claude Opus 5.5` — reconcile if needed.
