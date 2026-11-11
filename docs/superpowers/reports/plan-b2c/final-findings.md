# Final review findings — one fix wave

Important 1 (docs + small hardening):
- README "Draaien op de NAS": a scenario/SPEED_FACTOR change requires `docker compose down` → `collect.sh` first if you want the data → `docker compose down -v` → `up -d --build`; a bare `up -d` again (a) recreates only `sim`, resets sim time → ECS `time_reversed` and `/health` 503 for the rest of the run while the TCP healthcheck stays green, and (b) restarts a finished `soak` which appends a second run to `samples.jsonl`. Document both (also under "Bekende beperkingen", plus: a NAS reboot ends the soak run quietly because `soak` is `on-failure`).
- Driver hardening: when `/health.uptime_s` in a new sample is lower than in the previous sample, log a warning and increment `ecs_resets_seen` (if not already) — no new file; keep it minimal.

Important 2 — deploy/collect.sh: a missing container must NOT write -1; omit the key so `report._c1` reports "niet gemeten"; add `set -euo pipefail`. Update tests/deploy test for the collector accordingly.

Important 3 — packages/soak driver `run()`: wrap each `tick`/`sample` in `try/except Exception: log.exception("driver iteration failed")` and continue (SystemExit/KeyboardInterrupt not caught). Test: a client whose `containers()` raises TypeError → run() with a tiny hours value still ends normally with a final sample.

Minor 4 — Dockerfile: runtime image `python:3.12-slim-bookworm` (match the uv bookworm builder). Also drop `--platform=linux/amd64` from the FROM lines (compose sets `platforms`).

Minor 5 — tests/opcua/test_opcua_port.py::test_connection_lost_fails_fast: the `< 0.5 s` wall-clock bound is flaky under full-suite load. Make it robust: assert the failure is fast relative to the configured timeout (e.g. `< 0.5 * CONNECT/CALL timeout`) with a floor of 2 s, or measure with a monotonic clock around only the failing call — keep the intent (fails fast, not after the full timeout).

Also from T5 deferred: driver skips building a batch when `status.get("connected") is False` (one line + test).

Covering tests: `uv run pytest tests/soak tests/deploy tests/opcua -q`, then `uv run pytest -q`. Commit with trailer lines:
Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011Q6oL3tzJGQycNuMVuzTtk
