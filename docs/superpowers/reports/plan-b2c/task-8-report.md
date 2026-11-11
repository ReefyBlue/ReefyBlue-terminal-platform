# Task 8 report — Mini-soak e2e

Status: DONE. Commit `fde81ee` — test(e2e): 10-minute mini-soak with chaos cuts and report evaluation.

## Files
- `tests/e2e/test_mini_soak.py` (new; `pytestmark = [e2e_opcua, slow]`)
- `tests/e2e/fixtures/soak_mini.yaml` (new; `deploy/scenarios/soak.yaml` with `speed_factor: 20`, only comments differ otherwise)
- `tests/e2e/opcua_procs.py`: `sim_and_gateway(..., extra_sim_args=None)` (appended to the sim command line); new context manager `chaos_proxy(target_port, every, for_s)` yielding `{"endpoint", "proxy", "events", "stop_cuts"}` — LinkProxy on a free port + `Schedule.run` in a daemon thread; `stop_cuts()` ends the schedule with the link up; exit closes the proxy. Imports re-sorted (ruff).
- `pyproject.toml`: marker `slow`.

## Design choices
- Speed mechanism: the fixture's `speed_factor: 20`; the sim gets only `--autostart` (no `--speed-factor`).
- Driver: `Driver(EcsClient(url), layout, low_water=20, batch=30, loop_s=5, sample_every_s=60, out_dir=log_dir).run(10/60)` in a daemon thread; driver logs go to `log_dir/driver.log`; `samples.jsonl` and `report.md` land in `log_dir`.
- Deviation (deliberate): `stop_cuts()` is called 60 s before the driver's end so no cut is in progress at the last sample (with a 120 s period, a 5th cut would land at ~590 s and leave LINK_LOST / unknown outcomes open, making the end-state assertions racy). Result: 4 cuts per run.
- Assertions: six criterion names in the rendered markdown; ≥ 10 samples; ≥ 3 cuts, all `recovered_s < 120`, `cut_open_since is None` at end; last sample has status and `needs_release == 0 and unknown_outcomes == 0`; every window of `evaluate(samples, None)` (6 h → one partial window, judged anyway) has `moves_per_h > 0`, plus every full 2-minute window of `evaluate(..., window_hours=2/60)` (stall detector); `status.done` growth ≥ 60; `rss_mb[-1] <= 1.5 * rss_mb[1]`; no `YARD_FULL` in `ecs.log`; ECS exit code 0; driver rc 0.

## Runs
| | Run 1 | Run 2 |
|---|---|---|
| Result | PASS (624.98 s) | PASS (620.39 s) |
| Samples | 12 | 12 |
| Cuts / recovery (s) | 4: 15, 15, 20, 15 | 4: 15, 15, 20, 15 |
| status.done per sample | 0,15,33,40,43,52,61,65,70,78,88,88 | 0,17,34,42,51,59,69,76,87,95,105,105 |
| done growth | 88 | 105 |
| moves/h (wall) | 527.2 | 629.7 |
| rss_mb [1] → [-1] | 76.9 → 77.5 | 77.1 → 78.2 |
| needs_release / unknown at end | 0 / 0 | 0 / 0 |
| YARD_FULL in ecs.log | 0 | 0 |

Other checks: default suite `uv run pytest -q` → 638 passed, 17 deselected; `uv run pytest -m e2e_opcua tests/e2e/test_blip_during_run.py` (another `opcua_procs` user) → pass; ruff clean on the new test (two pre-existing BLE001 in `opcua_procs.py` untouched).

## Concerns
- `uv run pytest -m e2e_opcua tests/e2e -q` (global constraint) now includes this ~10.5-min test; it stays green but the run gets much longer. Use `-m "e2e_opcua and not slow"` to skip it.
- Readiness under `--autostart`: the sim runs (with faults) while `sim_and_gateway`/`ecs_service` wait for "no FAULT/OFFLINE"; both runs started fine, but a fault during startup could in principle delay readiness up to the 60 s timeout.
- The last sample often repeats the previous `done` (end sample taken a few seconds after the last periodic one) — harmless.

## Rendered report.md (run 2)

# Soak-rapport

- Start: 2026-11-11T15:58:01.842418+00:00
- Einde: 2026-11-11T16:08:02.150452+00:00
- Duur: 0.17 uur
- Meetpunten: 12

## Criteria

| # | Criterium | Uitslag | Detail |
|---|---|---|---|
| 1 | Geen container-herstart | – | niet gemeten (geen restarts-bestand) |
| 2 | RSS stabiel | ✅ | laatste 78.2 MB vs tweede meetpunt 77.1 MB (limiet 92.5 MB) |
| 3 | Doorvoer stabiel | ✅ | alleen deelvenster, 629.7 moves/uur (geen ±20 %-toets) |
| 4 | Cuts hersteld | ✅ | 4 cut(s), langste herstel 20 s, geen LINK_LOST aan het eind |
| 5 | Geen vastzittend werk | ✅ | needs_release 0, unknown_outcomes 0 |
| 6 | UI bereikbaar | ✅ | ui_ok in 12/12 meetpunten |

## Vensters

| Venster | Van | Tot | Moves/uur | RSS (MB) | Offline | Needs release | Unknown |
|---|---|---|---|---|---|---|---|
| 1 (deel) | 2026-11-11T15:58:01.842418+00:00 | 2026-11-11T16:08:02.150452+00:00 | 629.7 | 78.2 | 0 | 0 | 0 |

## Cuts

| Cut | Begin | Herstel (s) |
|---|---|---|
| 1 | 2026-11-11T16:00:05.910326+00:00 | 15.0 |
| 2 | 2026-11-11T16:02:04.039001+00:00 | 15.0 |
| 3 | 2026-11-11T16:04:01.540725+00:00 | 20.0 |
| 4 | 2026-11-11T16:06:03.737281+00:00 | 15.0 |

## Kanttekeningen

- rss_mb is de piek-RSS (ru_maxrss) en kan alleen stijgen; criterium 2 vergelijkt het laatste meetpunt met uur 2 (bij een run korter dan 2 uur met het tweede meetpunt).
- ECS-herstarts gezien door de driver (ecs_resets_seen): 0.
- Moves/uur is gemeten in wandtijd (Δ status.done); YARD_FULL wordt niet in de samples vastgelegd en dus niet door dit rapport beoordeeld.

