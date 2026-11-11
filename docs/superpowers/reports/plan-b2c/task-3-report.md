# Task 3 report — Gateway idempotent `AssignJob`; reconnect log backoff

Status: DONE_WITH_CONCERNS (the blip e2e had 2 of 6 runs fail on a timing flake that already exists in the test and is not caused by this change; see Concerns)
Branch: feat/plan-b2c-deploy-soak
Commit: 5a19ebd `fix(gateway): AssignJob idempotent for a running/finished job; quieter reconnect logging`

## Changes
- `packages/gateway/src/terminal_gateway/mirror.py`
  - New `_job_id(job_json)`: `json.loads(job_json)["job_id"]`. It returns `None` when the input isn't JSON, isn't an object, has no key, or the id isn't a string.
  - `EquipmentMirror._assign`: if the job id equals the mirrored `CurrentJobId` or `LastJobId`, it logs `info "assign %s: already %s upstream, idempotent True"` and returns True without forwarding. Otherwise it forwards as before. Unparseable JSON is forwarded and the upstream rejects it.
  - New `_mirrored(name)`: reads the mirror's own variable with `get_var(self.obj, name).read_value()`. The mirror keeps no separate cache of these values: the subscription handler writes them straight into `self.obj`. On a read error it logs and returns `None`, and the job is then forwarded. Empty ids (no job) never match.
- `packages/ecs_service/src/terminal_ecs_service/core.py`
  - New constants `RECONNECT_LOG_FIRST = 4` and `RECONNECT_LOG_EVERY_S = 30.0` (the second is exported). There is also a module-level `_monotonic = time.monotonic` hook for tests.
  - `connect_once` → `_log_unavailable(e)`: counts consecutive failures. Attempts 1–4 log at info. After that, at most one info line per 30 s period, counted from the outage's first attempt; all other attempts log at debug. The message is now `"equipment link not available (attempt N): …"`.
  - The counter resets after a successful `connect`. The retry timing (`CONNECT_RETRY_S`, `run()`) is unchanged.

## Tests (TDD)
- `tests/gateway/test_assign_idempotent.py`: a real in-process asyncua `Server` with an `EquipmentMirror` whose `_launch` is stubbed out, plus a `CountingUpstream` stand-in for `_uobj` that counts `call_method` calls. Tests:
  - `test_assign_idempotent_on_current_job` (also checks the info log)
  - `test_assign_idempotent_on_last_job`
  - `test_assign_forwards_new_job`
  - `test_assign_forwards_when_nothing_mirrored` (also covers malformed JSON)
  - RED: the 2 idempotent tests failed (AssignJob was forwarded); the 2 forward tests passed. GREEN: all pass.
- `tests/ecs_service/test_reconnect_log_backoff.py`: a fake clock patched over `core_mod._monotonic`, with attempts `CONNECT_RETRY_S` apart driven through `connect_once`. Tests:
  - `test_constant`
  - `test_reconnect_logging_backs_off`: 20 attempts, elapsed 38 s → 4 + ⌊38/30⌋ = 5 lines
  - `test_reconnect_logging_long_outage`: 300 attempts → 4 + 19 lines
  - `test_reconnect_log_counter_resets_on_success`
  - RED: missing attribute at first. I then used `raising=False` to get behavioral RED: 20, 300 and 10 lines were logged where 5, 23 and 4 were expected. GREEN after the change.

## Verification
- `uv run pytest tests/gateway -q`: 21 passed.
- `uv run pytest tests/ecs_service -q`: 97 passed.
- `uv run pytest -q`: 576 passed, 1 failed. The failure was `tests/opcua/test_opcua_port.py::test_connection_lost_fails_fast` with an `OSError` (port flake). That code is untouched, and the test passes on re-run.
- `uv run pytest -m e2e_opcua tests/e2e/test_blip_during_run.py -q`: 6 runs, 4 fully green (2 passed) and 2 with `1 failed, 1 passed`. Both failures were in `test_sim_gateway_blip_on_sts_run_completes` at `_assert_reconciled`.
- `ruff`: no new findings. The 5 findings in the touched packages are the same as on the baseline.

## Concerns
1. **The blip e2e is flaky for a reason unrelated to this change.** In both failing runs the ECS log shows `STS-01 offline (UPSTREAM_DISCONNECTED); job None kept`: the cut at 10 s wall landed while STS-01 was idle, so there was no missed outcome to reconcile. The idempotent branch did not fire in those runs. With my change stashed, the same test failed 1 of 5 runs with the same signature (pytest-326). The test assumes the 10 s cut always hits a running job, and that assumption is timing-dependent. A sturdier fix would be to cut when STS-01 reports `CurrentJobId` non-empty, instead of after a fixed delay. That is out of scope here.
2. **The e2e runs never exercised the fix.** The `idempotent True` log line appeared in 0 of 6 gateway logs, and `CONTAINER_PLACED` never appeared in any ECS log, with or without the change. So the original CONTAINER_PLACED re-issue loop did not reproduce in these runs. The fix is covered by the unit tests only.
3. **`LastJobId` match means "already done".** A re-issue of a job id that the upstream *failed* would also get True. That is fine today because the ECS job ids are per attempt (`<cid>-<type>-<n>`, with the counter going up on each retry), so a retry has a new id. It would become a problem if job ids were ever reused for retries.
4. **Brief vs spec wording.** The spec (§4.6) and global constraints say "backoff 1, 2, 5, 10, then every 30 s". I followed the brief and the caller instead: attempts 1–4, then every 30 s. With the 2 s retry, attempts 1–4 land at about 0, 2, 4 and 6 s.

---

## Fix report (review round 1) — commit da4ac6d

Status: DONE. The blip e2e passed 3/3. One timing flake remains in `tests/opcua` (see Concerns).

### Findings and fixes
1. **IMPORTANT: a `LastJobId` match left the order hanging.** You were right, and my earlier concern 3 was wrong. A rejected assign is retried with the same job id, because `job_counts` only goes up when an assign is accepted. The ECS ignored the outcome of the job it had rejected. So answering True alone meant the order never got an outcome.
   - Fix in `mirror.py` `_assign`: on a `LastJobId` match, it now answers True and also re-emits `LastJobOutcome` through `_emit_outcome`.
     - `COMPLETED` → JOB_COMPLETED.
     - Anything else → JOB_FAILED with reason `FAULT_DURING_OUTAGE`. A value other than COMPLETED/FAILED is also logged as a warning.
   - On a `CurrentJobId` match it answers True and emits nothing, because the job's own outcome event will still arrive.
   - Both id reads, the outcome read and the emit now all run under `_write_lock`, so there is no torn read.
2. **SPEC GAP: tests were not on the gateway harness.** `tests/gateway/test_assign_idempotent.py` is rewritten on the `rig` fixture from `test_gateway.py`. It uses a real upstream `EquipmentServer` and a real gateway. The test sets the upstream's CurrentJobId/LastJobId/LastJobOutcome and waits for the subscription to mirror them. `AssignJob` is called over OPC UA, and the upstream's `AssignJob` calls are counted by wrapping `server._assign` before start.
   - Tests:
     - `test_assign_idempotent_on_current_job` (also checks the info log)
     - `test_assign_idempotent_on_last_job[COMPLETED|FAILED]`, which asserts the re-emitted outcome event
     - `test_assign_forwards_new_job`
     - `test_assign_forwards_when_nothing_mirrored_or_malformed`
   - RED: with the previous commit's mirror, the 2 last-job variants fail. With the pre-task mirror, 3 fail (current plus both last-job). GREEN: 5 passed.
   - The file has a file-level `# ruff: noqa: F811`, needed because the tests take the imported `rig` fixture as a parameter.
3. **Minor: counter reset point.** The reconnect-log counter now resets after a successful `bind`, not after `_connect` returns. New test `test_reconnect_log_counter_not_reset_by_failed_bind`: RED with the old placement, GREEN now.
4. **BLIP TEST.** `_assert_reconciled` now keys off the back-online lines:
   - Regex: `([\w.-]+) back online: (\w+) \(job ([\w.-]+)\)`. The ECS log is JSON, so `\S+` swallowed the opening quote. I caught this on the first ×3 attempt.
   - job None → nothing to check.
   - IDLE with job X → requires `reconciled X on <eid> from equipment: (COMPLETED|FAILED)`.
   - BUSY with job X → requires no `outcome of X on <eid> unknown` line.
   - The `resolve …: failed` check and `_assert_completed` are kept.
5. **Extra fix outside the listed files.** The ×3 runs then exposed a second race, in `test_ecs_gateway_blip_run_completes`. The runner's `GET /status` landed exactly at the cut. `guarded()` answered 503 (a tested B2a contract: `test_connection_error_in_endpoint_drops_link_and_503`), and `EcsClient._get` raised, which ended the run.
   - Fix, additive only:
     - New `EcsClient.status_if_connected()`, which returns `None` on 503.
     - `run._poll_until_done` retries on 503 and counts it as no sim-time progress, so a 503 that never clears still ends in the stall timeout.
   - The ECS endpoint behaviour is unchanged.
   - Tests in `tests/runner/test_run_http.py`:
     - `test_status_503_during_a_link_cut_is_retried`
     - `test_status_503_for_good_stalls_out`
   - RED: both raised HTTPStatusError. GREEN now.

### Verification
- `uv run pytest tests/gateway tests/ecs_service -q`: 120 passed.
- `uv run pytest tests/runner tests/tos_stub -q`: 77 passed.
- `uv run pytest -m e2e_opcua tests/e2e/test_blip_during_run.py -q` ×3: **3/3**, 2 passed each time.
  - Every run shows `STS-01 back online: IDLE (job C000x-DISCHARGE-1)` followed by `reconciled`.
  - The idempotent branch fired 0 times in these runs.
- `uv run pytest -q`: 580 passed, 1 failed. The failure is `tests/opcua/test_opcua_port.py::test_connection_lost_fails_fast`, a wall-clock `< 0.5 s` assertion. It passes 3/3 alone and 39/39 as a package. It also failed in the full suite before this fix round (as an OSError) and does not touch `_assign`.
- `ruff`: no new findings in touched files. The remaining ones are the same as before the change.

### Concerns
- `test_connection_lost_fails_fast` fails under full-suite load. I have not checked it against the pre-task baseline in a full-suite run.
- The e2e runs never exercised the idempotent and re-emit path; it is covered by the rig tests.
- Item 5 touches `packages/runner` and `packages/tos_stub`, which are outside the brief's file list. It was needed to get 3/3.
