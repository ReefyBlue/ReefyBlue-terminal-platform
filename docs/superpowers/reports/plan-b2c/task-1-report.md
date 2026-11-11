# Task 1 report — ECS service: `/health` extras, `GET /containers`, bounded memory

Status: DONE_WITH_CONCERNS (minor; see Concerns)
Commit: 95f879f `feat(ecs_service): /containers paging, rss/uptime in /health, bounded finished-order retention`
Branch: feat/plan-b2c-deploy-soak

## What changed

### `packages/ecs/src/terminal_ecs/orders.py`
- `ContainerView` frozen dataclass `(seq, container_id, state: open|done|failed, job_type: DISCHARGE|LOAD, yard_slot)`.
- `OrderRecord.change_seq: int = 0`: the per-record "seq bump on state change". It is a **separate counter** from
  `OrderRecord.seq`. `seq` is the FIFO key: it changes on every stage entry and is reset to `submit_seq` on rollback, so it
  is not monotonic and can't serve as a cursor. Changing it would also change dispatch order.

### `packages/ecs/src/terminal_ecs/dispatcher.py`
- `_finished: dict[cid, OrderRecord]` holds DONE and FAILED records in finish order. `_retire()` adds to it from `_finish`
  and from `_fail_order` (only when the record was really open). `_changed()` bumps `_change_hw` and sets
  `rec.change_seq`. It runs on submit, done and failed.
- `_done_total` is cumulative, and `status()["done"]` now reads it instead of `len(self.completed)`.
- `is_known(cid)`: true for an open order or a retained finished one.
- `prune_done(keep) -> int` drops the oldest finished records beyond `keep`. Their container ids leave `_known`, so they
  can be submitted again. Their `_job_log` entries are removed: the ids are rebuilt from `job_counts`
  (`<cid>-<type>-<1..n>`), which is exactly the set `_issue` keeps. The head of `completed` (the pruned DONE ones) is
  deleted. `ValueError` if `keep < 0`.
- `containers_since(since, *, limit=None) -> (next, items)`:
  - It merges open records (already ascending by `change_seq`) with the tail of `_finished` above `since`, also
    ascending. It walks back from the end, so the cost is O(open + returned).
  - `next` is the last returned seq. With nothing to return it is the newest seq handed out (`_change_hw`), which also
    covers a cursor from beyond this dispatcher's history.
  - `ValueError` if `since < 0`.
- Module docstring: a paragraph on retention. Nothing in the dispatcher calls `prune_done`.

### `packages/ecs_service`
- `core.py`:
  - `KNOWN_RETENTION = 10_000` and `EcsCore(known_retention=...)`.
  - `rss_mb()` returns `ru_maxrss/1024` on Linux (bytes→MiB on darwin).
  - New methods: `uptime_s()`, `is_known(cid)` (delegates to the dispatcher; False before the first connect) and
    `containers(since, limit)`.
  - `known_containers` is removed. The dispatcher puts ids in `_known` before its first tick, so the "taken but the
    tick lost the link" cases still count as duplicates.
  - `step()` calls `_prune()` once per 60 s boundary, after `publish_new_jobs()` so a pruned order's jobs have all been
    published. When something was pruned, `_seen_jobs` is intersected with `job_log`.
- `app.py`:
  - `GET /containers?since=N` (`Query(0, ge=0)`, so a negative or non-int value gives 422). Page size is
    `CONTAINERS_PAGE = 500`. It needs no link and returns `{"next":0,"items":[]}` before the first connection.
  - Every `/health` answer (200 and both 503s) now carries `rss_mb` and `uptime_s`.
  - The duplicate check uses `core.is_known`.
- `schemas.py`: `HealthOut.rss_mb` and `HealthOut.uptime_s` (optional, so the change is additive), plus `ContainerOut`
  and `ContainersOut`.

### `packages/kpi/src/terminal_kpi/kpi.py` (not in the brief's file list; needed)
- `containers_done = float(status["done"])` replaces `len(dispatcher.completed)`. `status` was already computed there.
  This keeps the KPI cumulative after pruning, as the brief asks ("KPIs stay exact"). The value is identical when nothing
  is pruned, which is always true for the runner.

### Tests
- New `tests/ecs/test_prune_containers.py` (6 tests). It uses the FakePort in `tests/ecs/fake_port.py` (there is no
  `tests/ecs_service/fake_port.py`; the service tests import the same one through conftest).
  - `test_prune_keeps_open_and_latest_finished`:
    - 30 orders: 2 LOADs that fail with SLOT_EMPTY, plus 28 DISCHARGE. 25 are finished, then `prune_done(10)` returns 15.
    - The counters are unchanged (done 23 / failed 2 / open 5), and the view shows 5 open + 10 done.
    - The pruned jobs are gone from `job_log`.
    - Re-submitting a pruned id is accepted. A retained id and an open id both raise DUPLICATE.
  - `test_prune_is_noop_below_keep`
  - `test_container_view_fields`
  - `test_containers_since_cursor`, which also covers `limit` paging.
  - `test_containers_since_before_prune` (Review Focus 2): a stale cursor still gets the newest items and a usable
    `next`. A cursor beyond history gives `(hw, [])`. A negative cursor raises `ValueError`.
  - `test_runner_paths_never_prune`
- New `tests/ecs_service/test_containers_health.py` (5 tests):
  - `test_health_has_rss_and_uptime`: checks both 503 and 200.
  - `test_containers_endpoint_pages`: 520 orders give pages of 500 + 20, then an empty page with a stable `next`.
    Default `since=0`; 422 on `-1` and on `x`.
  - `test_step_prunes_finished_and_keeps_counting`: with retention 3, nothing is pruned between boundaries, and a prune
    happens at the boundary.
    - `done` and the KPI `containers_done` stay at 6.
    - `_seen_jobs ⊆ job_log` after the prune.
    - A pruned id is re-accepted (202). A retained id and an open id get a 422 duplicate.
    - The `/containers` order is checked.
  - `test_known_retention_default`
  - `test_is_known_tracks_submit`
- Updated existing tests, both for additive API changes:
  - `test_app.py::test_health_503_then_200` and `test_ecs_core.py::test_sim_time_going_backwards_flags_and_health_503`
    now use a subset match because `/health` has new fields.
  - Three `known_containers` assertions now use `is_known`.
- `tests/fixtures/ecs_sources.sha256` re-baselined in the same commit, with the reason in the commit body.

## TDD evidence
- RED: both new modules failed at collection with `ImportError` (`KNOWN_RETENTION` and `ContainerView` were missing).
- GREEN: `tests/ecs/test_prune_containers.py tests/ecs_service/test_containers_health.py` gave 11 passed.
- Before updating the old tests, the full suite showed the expected failures: 5 old tests (exact `/health` JSON and
  `known_containers`) plus the architecture hash. All were fixed as described above.

## Verification
- `uv run pytest -q`: 558 passed, 16 deselected (4m02s).
- `uv run pytest tests/e2e/test_service_equivalence.py tests/e2e/test_faults_equivalence.py -q`: 2 passed. Both are
  marked `e2e`, not `e2e_opcua`, so the command in the brief (`-m e2e_opcua …`) deselects them. They also run as part of
  the default suite, so they were run that way as well.
- `uv run pytest -m e2e_opcua tests/e2e -q`: 5 passed, 1 failed (`test_blip_during_run::test_sim_gateway_blip_on_sts_run_completes`).
  Re-running that file gave 2 passed. This is the known flake parked for B2c: "assign-during-cut job-id reuse" (B2d
  ledger line 66), which Task 3 fixes. It is a runner-over-OPC UA test, and the runner never calls `prune_done`.
- `ruff check` on the touched packages and tests is clean apart from 2 findings that were already there
  (`ecs_service/port.py` I001, `tests/ecs/test_dispatcher_discharge.py` RUF100).
- e2e_ui (Playwright) was not run. The viewer is untouched, it does not read `/health`, and its `containers_done` KPI
  value is unchanged.

## Self-review notes / concerns
1. **`rss_mb` is the *peak* RSS** (`ru_maxrss`), as the spec and brief prescribe. It can't go down, so it shows growth
   but not current usage. That is fine for the soak's growth criterion.
2. **Service KPIs and pruned history**:
   - `agv_wait_tp_s_mean` iterates `job_log`. After pruning, it only covers the retained window's jobs.
   - The KPI collector's own lists (`_travel`, `_wait_sts`, `_tp_arrival`) still grow without bound. That is outside
     this task; with about 3 000 moves in 72 h it is small.
   - A re-submitted pruned container id reuses job ids (`<cid>-<type>-1`). A stale `_tp_arrival` entry under that job
     id in the KPI collector could in theory pair wrongly. This is negligible with a 10 000-order retention.
3. **`containers_since` with a cursor beyond history** returns `next = newest seq`, with empty items. A client can detect
   that `next < since` and resynchronise from 0, which is relevant to the Task 5 driver after an ECS restart.
4. `_fail_order` now only retires a record it actually removed from `_open`. Today it is never called twice, and
   `_failed` still counts exactly as before.
5. **Prune cost**: once retention is full, each 60 s boundary drops only the few orders that finished since the last
   boundary. `_seen_jobs.intersection_update(job_log)` is O(job_log), about 60 k ids at most, so a few ms per prune.
   Task 2 replaces `_seen_jobs` with a deque anyway.
