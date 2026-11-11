# Plan B2c — final-review fix report

Branch `feat/plan-b2c-deploy-soak`, commit `91cf659` (one commit).

## Important 1 — README + driver uptime hardening

- README.md:284 (`SCENARIO` row) points at the new paragraph; README.md:318 **Ander scenario of `SPEED_FACTOR`**: `stop soak` → `./collect.sh` (optional) → `down -v` → `up -d --build`, plus what goes wrong with a bare `up -d` (a: only `sim` recreated, sim time resets, ECS `sim_time_reversed`, `/health` 503 for the rest of the run while the TCP healthcheck stays green; b: a finished `soak` restarts and appends a second run to `samples.jsonl`).
  Deviation: `collect.sh` runs *before* `down`, not after — after `down` the RestartCounts are gone (README already said so), so criterion 1 would be "niet gemeten".
- "Bekende beperkingen" (README.md:337-339): the bare-`up -d` pitfall, the NAS reboot ending the run quietly (`soak` is `on-failure`), and the missing-container → "niet gemeten" behaviour.
- driver.py:245 `_check_uptime`, called from `sample()` (driver.py:261): uptime_s lower than in the previous sample → WARNING + `ecs_resets_seen += 1`, unless `/containers` already counted a reset since the previous sample (`_resets_at_last_sample`).
- Tests: tests/soak/test_driver.py:304 `test_sample_counts_uptime_regression_as_ecs_reset` (failed before fix), :323 `test_uptime_regression_not_double_counted_after_containers_reset` (guard).

## Important 2 — collect.sh

- deploy/collect.sh:20-30: missing container → key left out + stderr note `let op: geen container voor <svc> (niet gemeten)`; no `-1` (which `report._c1` treated as a restart, since `-1` is truthy).
- deploy/collect.sh:6-7: `set -eu` plus `set -o pipefail` **where the shell supports it**. `/bin/sh` here is dash, which rejects `set -o pipefail` ("Illegal option"), so a plain `set -euo pipefail` would break the script under `sh`.
- Tests: tests/deploy/test_compose.py:151 `test_collect_sh` now has no chaos container (expects `{"sim":0,"gateway":0,"ecs":2,"soak":0}` + the stderr note) and runs under both `sh` and `bash` (pipefail active). It failed before the fix. :178 `test_collect_sh_fails_on_docker_error` (docker exits 3 → script non-zero, no report). :193 `test_report_treats_missing_restart_key_as_not_measured`.

## Important 3 — run() survives iteration errors

- driver.py:285 `_guarded()` (`except Exception` → `log.exception("driver iteration failed (...)")`). Used for `resume`, every `tick`, and every `sample` in `run()` (driver.py:300-315). SystemExit/KeyboardInterrupt are not caught. `resume` is guarded too: otherwise the named test (containers() raising TypeError) dies in `resume()` before the loop starts.
- Test: tests/soak/test_driver.py:283 `test_run_survives_unexpected_errors` (failed before fix with TypeError): run() returns 0 and writes the start and final samples.

## Minor 4 — Dockerfile

- deploy/Dockerfile:6 `FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder`, :20 `FROM python:3.12-slim-bookworm AS runtime`; `--platform` dropped (compose `platforms: [linux/amd64]`); header comment updated.
- Test: tests/deploy/test_compose.py:199 `test_dockerfile_stages_are_bookworm_without_platform_pin`.

## Minor 5 — test_connection_lost_fails_fast

- tests/opcua/test_opcua_port.py:268-282: bound = `max(2.0, 0.5 * CALL_TIMEOUT_S)` (= 2.0 s, asserted `< CALL_TIMEOUT_S` = 3 s). Each failing call is timed on its own with `time.monotonic()` inside the worker thread, so the thread hop and `now()` are not counted.

## T5 deferred — skip batch when disconnected

- driver.py:203: `_submit` returns before `make_batch()` when `status.get("connected") is False`.
- Test: tests/soak/test_driver.py:294 `test_submit_skips_batch_when_status_says_disconnected` (failed before fix): no batch posted, `_batch_no` still 0.

## Commands

```
$ uv run pytest tests/soak tests/deploy tests/opcua -q
102 passed in 62.88s
$ docker compose -f deploy/docker-compose.yml --env-file deploy/.env.example config >/dev/null; echo $?
0
$ uv run pytest -q
649 passed, 17 deselected in 259.26s (0:04:19)
$ uv run pytest tests/soak tests/deploy -q     # after lint fix (B023 lambda binding, check=False)
63 passed in 1.68s
$ uv run ruff check packages/soak tests/soak tests/deploy tests/opcua
11 findings, all already present before this change (the 3 new ones I introduced are fixed)
```

## Notes

- Commit trailer: I used `Claude Opus 5.5` from the session's attribution instructions, not `Claude Fable 5.1` from the findings file. The Claude-Session line is the same.
- The full suite ran before the final small lint edit (binding `now` as a lambda default, `check=False` in one test). After that edit, only soak and deploy were re-run (63 passed).
