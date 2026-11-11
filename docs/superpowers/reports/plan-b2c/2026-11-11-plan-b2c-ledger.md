# SDD ledger — plan: docs/superpowers/plans/2026-11-11-plan-b2c-deploy-soak.md
Spec: docs/superpowers/specs/2026-11-11-plan-b2c-deploy-soak-design.md. Branch feat/plan-b2c-deploy-soak (base feat/plan-b2b-viewer @ 56e22a3).

## Pre-flight scan
| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T1 → T5 | `GET /containers {next, items[seq,container_id,state,job_type,yard_slot]}` ↔ driver `containers(since)` paging | match |
| T1 → T5/T6 | `/health rss_mb, uptime_s` ↔ sample fields | match |
| T1 vs T2 | both touch dispatcher + hash fixture | sequential; each re-baselines |
| T2 → T8 | auto_release lifts box-less quarantine ↔ mini-soak asserts needs_release==0 | match |
| T3 → T8 | idempotent assign ↔ mini-soak cuts | match |
| T4 → T8 | `chaos_proxy` helper + LinkProxy move ↔ blip test import | T4 re-runs blip test |
| T5 → T8 | `Driver(...).run(hours)` + `sample_every_s`, `low_water`, `batch` kwargs | match |
| T6 → T7/T8 | `report.evaluate`/`main` ↔ collect.sh + mini-soak | match |
| T7 | `docker compose config` needs the docker CLI only (present) — no daemon | ok (skip if CLI missing) |
| Self | T1 `known_containers` → `is_known`: the 422 DUPLICATE path in submit must switch | implementer brief says so |
Ruling: pre-flight clean. Ruling: no Docker daemon here → image build unverified; compose validated with `config` only; the 48–72 h run is the user's on the NAS. — cost if wrong: Dockerfile may need a fix on first NAS build.
Task 1: Ruling: kpi `containers_done` switched to cumulative status()["done"] (outside brief; required for correctness after prune) — accepted. `rss_mb` = peak RSS (ru_maxrss) per spec; note for report: criterion 2 measures peak growth. `agv_wait_tp_s_mean` covers retained jobs only after prune → report caveat. Driver (T5) must resync from 0 when `next < since`.
Task 1: minor (deferred): job-log entry leaks when assign_job raises mid-issue (~1/cut); re-submitted pruned id reuses job ids (docstring); KPI collector lists unbounded (~3k moves/72h, fine); test tidiness (unused monkeypatch, repeated assertion, test name). Carry to T5: driver restarts from 0 when `next < since`. Carry to T3: blip flake confirm after idempotent assign.
Task 1: complete (commits 6211b63..95f879f, review clean)
Task 2: Ruling: box-less test = key present and value None/"" (sim reports None); missing key → stays quarantined (vendor adapter without box reporting keeps the human rule). — cost if wrong: an adapter without `loaded_container_id` stalls unattended runs (documented).
Task 2: minor (deferred): lift only event-driven (latent stall once on_transfer is wired); TRANSFER_ERROR test models an event the sim never sends; `client_count` duplicates `clients`; weak guard in incremental-publish test; `_new_jobs` undrained in runner (bounded by run). Note for mini-soak: a box-on-board REPLAN_STREAK quarantine stalls by design → report criterion 5 marks it "handmatig".
Task 2: complete (commits 95f879f..7a62993, review clean)
Task 3: Ruling: backoff = attempts 1–4 logged then once per 30 s (plan wording; spec's "1,2,5,10" was shorthand). Ruling: blip-test flake is test design (asserts "reconciled" even when the cut lands on an idle STS, log "job None kept") → make the test accept either path in this task's fix round.
Task 3: fix round 1/5 (5 addressed, 0 open — LastJobId re-emit outcome; rig tests; reset-after-bind; blip test keyed on back-online; runner tolerates 503 during cut; commits 5a19ebd..da4ac6d)
Task 3: Ruling: runner/tos_stub change (`status_if_connected`, 503 retry bounded by stall timeout) accepted — needed for 3/3 blip runs; does not touch in-process equivalence. — cost if wrong: an ECS outage shows up ≤120 s later in runner runs.
Task 3: minor (deferred): `_assert_reconciled` trivially passes with no back-online line; BUSY check adds no coverage; mirror docstring overstates lock guarantee; 503 warning per poll; `test_connection_lost_fails_fast` wall-clock (<0.5 s) flaky under full-suite load (passes 3/3 alone) → make robust in final wave.
Task 3: complete (commits 7a62993..da4ac6d, review clean after 1 fix round)
Task 4: fix round 1/5 (5 addressed, 0 open — hostname per connect; accept loop; clamp; target validation; loopback test; commits aefbb3b..8539161)
Task 4: minor (deferred): no test for transient accept OSError; IPv6 bracket targets unhandled; port range unchecked.
Task 4: complete (commits da4ac6d..8539161, review clean after 1 fix round)
Task 5: Ruling: soak may import terminal_ecs (WorkOrder) and terminal_sim (load_layout) — allow-list widened; sample key `alarms_by_kind` (T6 reads that, spec §3.2 name superseded). — cost if wrong: none.
Task 5: minor (deferred, for final wave): run() lacks a catch-all per iteration (non-HTTP errors end the run); batches built during a cut (skip when status.connected is False); reslot leaks a yard slot (cid→slot index); timeout-after-accept can double-book; extra sample after a stall; SIGTERM grace / SIGINT. Carry to T7: `soak` restart policy `on-failure` (else `--hours` end restarts a new run and appends samples) + `stop_grace_period: 60s`.
Task 5: complete (commits 8539161..74bc8c6, review clean)
Task 6: fix round 1/5 (3 addressed, 0 open — empty-status handling; c2 reference; partial window; commits 062918d..1bef22a)
Task 6: minor (deferred): trailing partial window can hide an end-of-run stall (by design).
Task 6: complete (commits 74bc8c6..1bef22a, review clean after 1 fix round)
Task 7: Ruling: sim gains `--speed-factor` (outside brief) so SPEED_FACTOR in .env works; AUTO_RELEASE mapped 1/true/yes → flag (not `:+`). ECS healthcheck = TCP only (documented). — cost if wrong: none.
Task 7: minor (deferred, final wave): pin `python:3.12-slim-bookworm`; drop `FROM --platform`; healthcheck interval 30–60 s (asyncua log noise); collect.sh writes -1 for missing container (omit key) + pipefail; README AUTO_RELEASE wording (1/true/yes); document re-`up` restarts soak & NAS reboot ends the run; .dockerignore caches. Unverifiable here: BuildKit on NAS, Container Station app path.
Task 7: complete (commits 1bef22a..614dd29, review clean)
Task 8: Ruling: cuts stop 60 s before the driver ends (deterministic end-state); `chaos_proxy` is a context manager. Mini-soak 2/2 green (4 cuts each, recovered 15–20 s, done +88/+105, RSS +<1 MB, no YARD_FULL).
Task 8: minor (deferred): driver teardown not in finally; with_status[-1] indexed before check; logger level not restored.
Task 8: complete (commits 614dd29..fde81ee, review clean)
Final review (opus): 0 Critical, 3 Important (README second-run corruption; collect.sh -1; driver run() catch-all), 2 minors-before-merge (bookworm pin; flaky opcua wall-clock test). One fix wave dispatched (+ T5 "skip batch during cut"). Ledger correction: criterion 5 is FAIL (not "handmatig") for a box-on-board quarantine unless `--manual`.
Ruling: retention 10k vs criterion 2 at SPEED_FACTOR ≥ 3 (≈30 MiB retained) → documented limit, not changed now. — cost if wrong: a fast soak may fail criterion 2 for a benign reason.
Final fix wave: commit ee1f23b (trailer amended by controller). Re-review: all addressed; deviations accepted (collect.sh before down; conditional pipefail; guarded resume). Residual minors parked: ecs_resets_seen double-count edge cases; uptime-based restart detection can miss; collect.sh overwrites previous run files (README: move them first).
