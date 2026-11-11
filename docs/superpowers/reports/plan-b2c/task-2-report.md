# Task 2 report — box-less quarantine under auto_release; publish efficiency

Status: DONE_WITH_CONCERNS (one deliberate deviation from the brief's literal predicate; see Concerns)
Commit: 7a62993 `feat(ecs): auto_release lifts box-less quarantine; incremental job publishing` (branch feat/plan-b2c-deploy-soak)

## Changes
- `packages/ecs/src/terminal_ecs/dispatcher.py`
  - `_apply_state` IDLE branch: under `auto_release`, a quarantined AGV is lifted when `_box_free(eid)`. Because `_come_back` ends in `_apply_state`, this covers both a normal IDLE report and a return from OFFLINE as IDLE. Spec §4.2 says "as soon as the device reports IDLE", not only on link return.
  - `_box_free`: `"loaded_container_id" in extra and not extra["loaded_container_id"]`.
  - `_lift(eid, by)`: the shared core of `release()` (clear release, exclusion and quarantine, with its log). `release()` keeps its checks and log text ("quarantine of X lifted by operator release"). I did not call `release()` itself from event handling, because it re-reads the state and could raise `ReleaseRefused` inside an event callback.
  - `_new_jobs: deque[LoggedJob]`: appended where `_job_log[...]` is set in `_issue`. `new_jobs_since_last_publish()` drains it and skips entries that are no longer the live log entry (rejected by `assign_job`, or pruned).
  - Module docstring (QUARANTINE, state sets) updated.
- `packages/ecs_service/.../core.py`: `publish_new_jobs` now drains the deque, and the message is identical. `_seen_jobs` is removed, along with its pruning in `_prune`. `publish_status` returns early when `broadcaster.client_count == 0`. Module docstring updated.
- `packages/ecs_service/.../events.py`: `Broadcaster.client_count` (the existing `clients` is kept).
- Docs: spec B2d §5.3a got one sentence. In the README B2d section, the new rule sentence replaces "Een AGV in quarantaine blijft ook met --auto-release wachten". I also qualified two sentences that would otherwise contradict it: the "ook met --auto-release" in the Herhaalde-mislukkingen paragraph, and the closing "Let op" sentence, which now says "met een box aan boord".
- `tests/fixtures/ecs_sources.sha256` re-baselined. The reason is in the commit body.
- Tests:
  - `tests/ecs/fake_port.py`: added `set_extra(eid, **values)`. It is additive; the default extra is unchanged.
  - New `tests/ecs/test_quarantine_auto_release.py` (6 tests: lift with `""` and `None`; lift on link return; stays with a box; stays when the load is not reported; stays without auto_release, then operator release works).
  - New `tests/ecs_service/test_publish_efficiency.py` (5 tests: `client_count`; the deque drains in order; rejected jobs are skipped; `test_publish_new_jobs_is_incremental` with 1336 logged jobs, where a tick with one new job publishes exactly one message and the drain is empty afterwards, and a guard asserts `job_log` is not read during `publish_new_jobs`; `test_publish_status_skips_without_clients` spies `status_message`).
  - `tests/ecs_service/test_containers_health.py`: the `_seen_jobs` assertion is replaced by "deque drained".

## OPC UA check
`loaded_container_id` reaches `EquipmentSnapshot.extra` on the ECS side, so no codec change was needed:
- The sim AGV's `extra()` includes it.
- `nodeset.py` writes the full extra as the `Extra` JSON variable (and also `LoadedContainerId` via `EXTRA_KEY_MAP`).
- `Extra` is in `EQUIPMENT_VARS`, which the gateway mirror copies verbatim.
- `OpcUaPort.snapshot` parses `Extra` back into `extra`.

## Evidence
- RED: 9 failed, 2 passed. The two passing were the "stays" tests that pin current behaviour. One initial false failure came from a fixture bug: `make_port(n_agv=1)` still creates AGV-02. I fixed it with a single-AGV FakePort, after which the 3 lift tests failed as expected.
- GREEN: `tests/ecs tests/ecs_service` 191 passed.
- Equivalence: `tests/e2e/test_service_equivalence.py tests/e2e/test_faults_equivalence.py` 2 passed (exact).
- Full default suite `uv run pytest -q`: 569 passed, 16 deselected. After a final docstring-only rewrap and re-baseline I re-ran the architecture, ecs, ecs_service and equivalence tests: 232 passed.
- `uv run pytest -m e2e_opcua tests/e2e -q`: 5 passed, 1 failed (`test_baseline_opcua_matches_inprocess`). Re-run on its own it passed (177 s), so it looks like a timing flake and not this change.
- e2e_ui: not run.
- ruff on the changed and new files: clean.

## Concerns
1. **Deviation from the brief's predicate.** The brief says `extra.get("loaded_container_id", "") == ""`, but this does not work as written:
   - The sim reports an empty AGV as `None` (JSON `null` over OPC UA), not `""`. That predicate would never lift in the sim.
   - Its default `""` treats a missing key as "empty". That would also lift quarantines in the existing B2d tests, whose FakePort has no such key (`test_transfer_error_quarantines_agv`, `test_replan_streak_with_box_on_agv_quarantines_it`).
   - I implemented "key present and `None` or `""`". A device that doesn't report its load stays quarantined, which is the safer choice. If the controller wants the literal rule, those B2d tests need changing.
2. A box-less quarantine is only lifted at the *next* IDLE report. An AGV quarantined while already IDLE stays quarantined until it reports IDLE again (spec wording: "zodra het apparaat IDLE meldt"). In the sim, the quarantine paths (JOB_FAILED → IDLE; TRANSFER_ERROR with the box actually on board) make this mostly moot.
3. `_new_jobs` is drained only by the service. In-process runner runs never drain it, so it grows alongside `job_log` (references only; `job_log` already grows unbounded there).
4. Out of scope, but relevant for the 72 h soak: `terminal_kpi.kpi._tp_wait` iterates the whole `job_log` (O(N) per KPI snapshot). It is bounded only by `prune_done` pruning job-log entries.
5. README line 264 (viewer section) still says "Een AGV in quarantaine vraagt sowieso een mens". I left it because that context assumes no `--auto-release`.
