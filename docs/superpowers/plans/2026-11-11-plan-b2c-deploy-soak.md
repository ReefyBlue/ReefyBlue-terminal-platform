# Plan B2c — Deploy and Soak Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the whole stack as a `docker compose` application on the QNAP and prove 48–72 h unattended stability with a soak driver, chaos cuts and a pass/fail report.

**Architecture:** New package `terminal_soak` (chaos proxy, work driver, report). The ECS service grows `GET /containers`, `rss_mb`/`uptime_s` in `/health`, and bounded memory; the dispatcher lifts box-less quarantines under `auto_release`; the gateway makes `AssignJob` idempotent. `deploy/` holds one Dockerfile, the compose file, env template, soak scenario and collector script. A 10-minute mini-soak e2e test exercises driver + chaos + report against local processes.

**Tech Stack:** Python 3.12, uv, FastAPI, httpx, asyncua, Docker Compose (file validated with `docker compose config`; no daemon available in the dev workspace).

**Spec:** `docs/superpowers/specs/2026-11-11-plan-b2c-deploy-soak-design.md`

## Global Constraints

- Existing API additive only; all B2a/B2b/B2d tests stay green (`uv run pytest -q`, and `uv run pytest -m e2e_opcua tests/e2e -q`, and `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -q`).
- Equivalence fixtures (`tests/e2e/fixtures/*.json`) must stay exact; any change under `packages/ecs/src` requires re-baselining `tests/fixtures/ecs_sources.sha256` in the same commit with the reason in the commit body.
- Soak defaults: `LOW_WATER=40`, `BATCH=60`, driver loop 10 s, sample every 3600 s (`--sample-every`), `--hours 72`, chaos `--every 14400 --for 30`, `KNOWN_RETENTION=10000`, reconnect log backoff 1, 2, 5, 10 then every 30 s.
- Container ids from the driver: `SOAK-<batch>-<i>` (batch from 1, i from 0).
- Image: `python:3.12-slim` + uv, `linux/amd64`, non-root `USER app`, `PYTHONUNBUFFERED=1`; compose: `restart: unless-stopped`, `logging: json-file max-size 20m max-file 5`, ECS published on `8000:8000` only.
- Commit trailer lines: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_011Q6oL3tzJGQycNuMVuzTtk`.
- Branch `feat/plan-b2c-deploy-soak` (base `feat/plan-b2b-viewer`).

## Review Focus

1. Driver restarted mid-soak against an ECS that already has open work — it must rebuild `occupied` from `GET /containers?since=0` and continue, never refuse (Task 6 test `driver_resumes_from_containers`).
2. `GET /containers` after pruning — a `since` older than the retained window must still return the newest items and a `next` cursor, never 500 (Task 1 test `containers_since_before_prune`).
3. Quarantined AGV with a box still on board under `auto_release` — must stay quarantined (Task 2 test `quarantine_with_box_stays_under_auto_release`).
4. `AssignJob` re-issued after a timeout for a job the device already finished (`LastJobId` matches) — must answer True and not re-execute (Task 3 test `assign_idempotent_on_last_job`).
5. Report on a samples file with a single sample (driver killed early) — must emit "niet gemeten" rows, not divide by zero (Task 6 test `report_single_sample`).

---

### Task 1: ECS service — `/health` extras, `GET /containers`, bounded memory

**Files:**
- Modify: `packages/ecs/src/terminal_ecs/dispatcher.py` (+`prune_done(keep: int) -> int`, `containers_since(since: int) -> tuple[int, list[ContainerView]]`, per-record `seq` bump on state change), `packages/ecs/src/terminal_ecs/orders.py` (+`ContainerView` frozen dataclass `seq, container_id, state, job_type, yard_slot`), `packages/ecs_service/src/terminal_ecs_service/{core.py,app.py,schemas.py}`, `tests/fixtures/ecs_sources.sha256`
- Test: `tests/ecs/test_prune_containers.py`, `tests/ecs_service/test_containers_health.py`

**Interfaces:**
- Produces: `GET /health` → existing fields + `rss_mb: float` (`resource.getrusage(RUSAGE_SELF).ru_maxrss/1024` on Linux) + `uptime_s: float`; `GET /containers?since=N` → `{"next": int, "items": [{seq, container_id, state: "open"|"done"|"failed", job_type: "DISCHARGE"|"LOAD", yard_slot}]}` ordered by `seq`, at most 500 items per call; `Dispatcher.prune_done(keep=KNOWN_RETENTION)` drops the oldest finished records (done+failed) beyond `keep` from `completed`, `_job_log` entries of those orders and the container index; `EcsCore.known_containers` is replaced by `core.is_known(cid)` = open or retained-finished; `core.step()` calls `prune_done` once per 60 s boundary; `_seen_jobs` pruned alongside.

- [ ] **Step 1: Failing tests** — `test_prune_keeps_open_and_latest_finished` (submit 30 orders on the in-process port, finish 25, `prune_done(keep=10)` → `status()["done"]` unchanged (counter is cumulative), `containers_since(0)` lists 5 open + 10 finished, and a re-submit of a pruned id is accepted while a re-submit of a retained one raises DUPLICATE); `test_containers_since_cursor` (`next` advances; second call with `next` returns only newer changes); `test_containers_since_before_prune` (Review Focus 2); service: `test_health_has_rss_and_uptime` (`rss_mb > 0`, `uptime_s >= 0`), `test_containers_endpoint_pages` (`?since=` default 0, max 500 items, 422 on negative).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** per Interfaces. `status()["done"]` keeps counting via an integer `_done_total` (currently `len(self.completed)`) so the KPIs stay exact; equivalence fixtures must remain byte-identical (prune only runs from the service `step`, never from the runner paths — assert that in a test by running `run_once` on `plan_a_baseline` scenario and diffing against the fixture, which the existing `test_faults_equivalence` already does).
- [ ] **Step 4: Run** — `uv run pytest tests/ecs tests/ecs_service tests/e2e/test_service_equivalence.py tests/e2e/test_faults_equivalence.py -q` → PASS; re-baseline the hash fixture (`uv run pytest tests/test_architecture.py -q` tells you how).
- [ ] **Step 5: Commit** — `feat(ecs_service): /containers paging, rss/uptime in /health, bounded finished-order retention`.

---

### Task 2: Dispatcher — box-less quarantine lifts under `auto_release`; service publish efficiency

**Files:**
- Modify: `packages/ecs/src/terminal_ecs/dispatcher.py` (`_come_back`), `packages/ecs_service/src/terminal_ecs_service/core.py` (`publish_new_jobs`, `publish_status`), `packages/ecs_service/src/terminal_ecs_service/events.py` (+`Broadcaster.client_count -> int`), `docs/superpowers/specs/2026-11-10-plan-b2d-link-loss-design.md` §5.3a (one sentence), `README.md` (B2d section, one sentence), `tests/fixtures/ecs_sources.sha256`
- Test: `tests/ecs/test_quarantine_auto_release.py`, `tests/ecs_service/test_publish_efficiency.py`

**Interfaces:**
- Consumes: `EquipmentSnapshot.extra["loaded_container_id"]` ("" when empty) from the sim/gateway.
- Produces: with `auto_release=True`, an AGV in `_quarantined` that comes back IDLE with `extra.get("loaded_container_id", "") == ""` is released (quarantine + needs_release cleared, alarm cleared); with a non-empty id it stays. `Dispatcher.new_jobs_since_last_publish() -> list[LoggedJob]` backed by a `deque` appended in `_log_job` and drained by the caller; `publish_status` returns early when `broadcaster.client_count == 0`.

- [ ] **Step 1: Failing tests** — `test_quarantine_without_box_lifts_under_auto_release`, `test_quarantine_with_box_stays_under_auto_release` (Review Focus 3), `test_quarantine_stays_without_auto_release`; `test_publish_new_jobs_is_incremental` (after 1 000 logged jobs, a tick with one new job publishes exactly one message and the drained list is empty afterwards); `test_publish_status_skips_without_clients` (no `status_message()` call when `client_count == 0` — spy via monkeypatch).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** full default suite + equivalence; re-baseline hash. **Step 5: Commit** — `feat(ecs): auto_release lifts box-less quarantine; incremental job publishing`.

---

### Task 3: Gateway idempotent `AssignJob`; reconnect log backoff

**Files:**
- Modify: `packages/gateway/src/terminal_gateway/mirror.py` (`_assign`), `packages/ecs_service/src/terminal_ecs_service/core.py` (`connect_once` / `run` logging)
- Test: `tests/gateway/test_assign_idempotent.py`, `tests/ecs_service/test_reconnect_log_backoff.py`

**Interfaces:**
- Produces: `_assign(job_json)` parses `job_id` (`json.loads(job_json)["job_id"]`), reads the mirrored `CurrentJobId` and `LastJobId` values; if either equals `job_id` it logs `info "assign %s: already %s upstream, idempotent True"` and returns True without forwarding. `EcsCore` reconnect: consecutive failed `connect_once` attempts log at `info` only on attempt 1, 2, 3, 4 and then every 30 s (attempt timing unchanged); a successful connect resets the counter.

- [ ] **Step 1: Failing tests** — `test_assign_idempotent_on_current_job`, `test_assign_idempotent_on_last_job` (Review Focus 4; use the existing gateway test harness in `tests/gateway/` with a fake upstream whose `AssignJob` counts calls), `test_assign_forwards_new_job`; `test_reconnect_logging_backs_off` (caplog: 20 failed attempts → 4 + ⌊elapsed/30⌋ info lines).
- [ ] **Step 2–5** as above. Commit — `fix(gateway): AssignJob idempotent for a running/finished job; quieter reconnect logging`.

---

### Task 4: `terminal_soak` package — `linkproxy` move and `chaos`

**Files:**
- Create: `packages/soak/pyproject.toml` (deps: `terminal-tos-stub`, `terminal-model`, `httpx`), `packages/soak/src/terminal_soak/{__init__.py,linkproxy.py,chaos.py}`
- Modify: `tests/e2e/opcua_procs.py` (import `LinkProxy` from `terminal_soak.linkproxy`; delete the local class), root `pyproject.toml` (workspace member, dev group, pythonpath), `tests/test_architecture.py` (`soak` in `PACKAGES`/`ALLOWED` = {terminal_model, terminal_tos_stub})
- Test: `tests/soak/test_chaos.py`

**Interfaces:**
- Produces: `terminal_soak.linkproxy.LinkProxy` (unchanged API: `LinkProxy(routes: dict[int,int], host="127.0.0.1").start()`, `cut()`, `restore()`, `stop()`); `terminal_soak.chaos.main(argv)` with `--listen PORT --target HOST:PORT --every S --for S --host 0.0.0.0`; `chaos.Schedule(every_s, for_s).run(proxy, stop_event)` (pure scheduling loop, injectable clock for tests); JSON log lines `{"event":"cut"|"restore","at":<iso>}`.

- [ ] **Step 1: Failing tests** — `test_schedule_cuts_and_restores` (fake clock: every=100, for=10 → cut at 100, restore at 110, cut at 200); `test_every_zero_never_cuts`; `test_proxy_import_path` (`tests/e2e/opcua_procs.LinkProxy is terminal_soak.linkproxy.LinkProxy`).
- [ ] **Step 2–5.** Also run `uv run pytest -m e2e_opcua tests/e2e/test_blip_during_run.py -q` to prove the moved proxy still works. Commit — `feat(soak): terminal_soak package with LinkProxy and chaos cutter`.

---

### Task 5: `terminal_soak.driver`

**Files:**
- Create: `packages/soak/src/terminal_soak/driver.py`, `packages/soak/src/terminal_soak/yard.py`
- Test: `tests/soak/test_yard.py`, `tests/soak/test_driver.py` (against `create_app(core, run_loop=False)` with the in-process `FakePort`/sim port as the ECS-service tests do; no subprocesses)

**Interfaces:**
- Consumes: `EcsClient` (`post_workorders`, `status`, `kpis`, `health`, `alarms()` — add `alarms()` and `containers(since)` and `health_json()` to `terminal_tos_stub.client.EcsClient` if missing: thin GET wrappers), `generate_workorders`, `SimpleSlotAllocator`, `GET /containers`.
- Produces: `yard.Yard(blocks)` with `occupy(slot, cid)`, `free(slot)`, `pick_free(block) -> slot|None`, `pick_occupied(block) -> (slot, cid)|None`, `occupied_count`; `driver.Driver(client, layout, *, low_water=40, batch=60, loop_s=10, sample_every_s=3600, out_dir)` with `resume()` (rebuild `Yard` from `containers(since=0)` paging), `tick(now)` (poll `/containers` since cursor → update yard; if `status.open < low_water` → `make_batch()` → `post_workorders`), `sample(now)` (JSONL line per spec §3.2 to `out_dir/samples.jsonl`), `run(hours)`; `make_batch()` returns DISCHARGE-only while `yard.occupied_count < batch//2`, else alternating; `main(argv)` with `--ecs --layout --hours --out --low-water --batch --loop --sample-every`.

- [ ] **Step 1: Failing tests** — `test_yard_balance` (occupy/free bookkeeping, pick order deterministic), `test_make_batch_discharge_only_on_empty_yard`, `test_make_batch_alternates_when_yard_has_boxes` (LOAD slots are occupied ones), `test_driver_submits_below_low_water` (status open 39 → one batch posted; 40 → none), `test_driver_resumes_from_containers` (Review Focus 1: ECS pre-loaded with 12 open + 8 done DISCHARGE orders → `resume()` yields 20 occupied minus LOADed), `test_sample_line_fields` (all keys from spec §3.2 present; `ui_ok` false when `/ui/` 404).
- [ ] **Step 2–5.** Commit — `feat(soak): work driver with yard balance, resume and hourly samples`.

---

### Task 6: `terminal_soak.report`

**Files:**
- Create: `packages/soak/src/terminal_soak/report.py`
- Test: `tests/soak/test_report.py` (synthetic `samples.jsonl` fixtures written by the test)

**Interfaces:**
- Produces: `report.evaluate(samples: list[dict], restarts: dict[str,int] | None) -> Report` with `criteria: list[Criterion(name, passed: bool|None, detail: str)]` for the six spec §1 criteria (`None` = "niet gemeten"), `windows: list[Window(start, end, moves_per_h, rss_mb, offline, needs_release, unknown)]` (6 h windows; shorter if the run is shorter), `cuts: list[Cut(at, recovered_s)]`; `report.render_markdown(Report) -> str`; `main(argv)`: `samples.jsonl [--restarts restarts.json] [--window-hours 6]` → markdown on stdout.

- [ ] **Step 1: Failing tests** — `test_all_pass_on_steady_samples`, `test_rss_growth_fails`, `test_throughput_drop_fails`, `test_cut_not_recovered_fails`, `test_restarts_none_is_not_measured`, `test_report_single_sample` (Review Focus 5), `test_markdown_has_six_criteria`.
- [ ] **Step 2–5.** Commit — `feat(soak): pass/fail soak report`.

---

### Task 7: `deploy/` — Dockerfile, compose, env, scenario, collector, docs

**Files:**
- Create: `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/.env.example`, `deploy/scenarios/soak.yaml`, `deploy/collect.sh`, `deploy/README.md`, `.dockerignore`
- Modify: `README.md` (new main section "Draaien op de NAS (B2c)" after "Webviewer (B2b)")
- Test: `tests/deploy/test_compose.py`

**Interfaces:**
- Produces: services/commands/ports/healthchecks exactly as spec §2 table; `.env.example` keys `SCENARIO=soak.yaml SPEED_FACTOR=1 SOAK_HOURS=72 CUT_EVERY_S=14400 CUT_FOR_S=30 AUTO_RELEASE=1 LOW_WATER=40 BATCH=60`; `AUTO_RELEASE` maps to the `--auto-release` flag via compose `command` with shell form (`sh -c '… ${AUTO_RELEASE:+--auto-release}'`); `collect.sh` writes `restarts.json` (`{"<service>": <RestartCount>}`) and copies `samples.jsonl` out of the volume, then runs `report.py` inside the image: `docker compose run --rm soak python -m terminal_soak.report /data/soak/samples.jsonl --restarts /data/soak/restarts.json > soak-report.md`.
- Dockerfile: stage `builder` (`ghcr.io/astral-sh/uv:python3.12-bookworm-slim`, `uv sync --frozen --no-dev --all-packages`), stage `runtime` (`python:3.12-slim`, copy `/app/.venv` + sources + `terminal.yaml` + `deploy/scenarios`, `USER app`, `WORKDIR /app`, `PATH=/app/.venv/bin:$PATH`).

- [ ] **Step 1: Failing test** — `tests/deploy/test_compose.py`: `docker compose -f deploy/docker-compose.yml --env-file deploy/.env.example config` exits 0 (skip if `docker` missing) and the rendered YAML has the five services, `ecs` publishes only `8000:8000`, every service has `restart: unless-stopped` and a healthcheck except `soak`, `depends_on` chain as spec; `deploy/scenarios/soak.yaml` loads with `load_scenario` and has faults on all three kinds and `discharge_share: 0.5`; `.env.example` has every key above.
- [ ] **Step 2–3: Implement**; `uv lock` unchanged (no new deps). README section: Container Station steps (upload compose + build context as "Create application" or `docker compose up -d --build` over SSH), how to watch (`/ui/`, `docker compose logs -f ecs`), how to finish (`deploy/collect.sh`), limitations spec §7.
- [ ] **Step 4: Run** `uv run pytest tests/deploy -q` → PASS. **Step 5: Commit** — `feat(deploy): Docker Compose stack for the QNAP and soak collector`.

---

### Task 8: Mini-soak e2e

**Files:**
- Create: `tests/e2e/test_mini_soak.py` (markers `e2e_opcua`, `slow`), `tests/e2e/fixtures/soak_mini.yaml` (soak.yaml with `speed_factor: 20`)
- Modify: `tests/e2e/opcua_procs.py` (+`chaos_proxy(target_port, every, for_s)` helper), root `pyproject.toml` (marker `slow`)

- [ ] **Step 1: Write the test** per spec §5: sim `--autostart` + gateway + chaos (every 120 s, for 15 s) + ECS `--auto-release` → chaos port; driver thread `Driver(...).run(hours=10/60)` with `sample_every_s=60`, `low_water=20`, `batch=30`; afterwards `report.evaluate(samples, None)`: assert ≥ 3 cuts all recovered < 120 s; last sample `needs_release == 0 and unknown_outcomes == 0`; every window `moves_per_h > 0`; `rss_mb[-1] <= 1.5 * rss_mb[1]`; ECS log has no `YARD_FULL`; `status.done` grew by ≥ 60.
- [ ] **Step 2: Run** `uv run pytest -m e2e_opcua tests/e2e/test_mini_soak.py -q` (≈11 min) → PASS; run twice; record both.
- [ ] **Step 3: Commit** — `test(e2e): 10-minute mini-soak with chaos cuts and report evaluation`.
