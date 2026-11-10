# Task 14 report: KPI-collector

Status: DONE

## Implemented
`packages/sim/src/terminal_sim/kpi.py`: `KpiCollector(port, dispatcher, *, faults_total=None)` with `sample()`, `finish(duration_s)`, `to_csv_row(path, run_id, kpis)`. Counting rules exactly as in the decisions. Does not import the sim world; `terminal_ecs` does not import it.

## Tests (tests/sim/test_kpi.py, 9)
moves_per_hour, utilisation_from_samples, utilisation_without_samples_is_zero, agv_wait_and_travel_split, agv_means_zero_without_transports, turnaround, turnaround_zero_without_moves, faults_replans_and_counts, csv_header_once.
Helper import: added `tests/sim/conftest.py` which puts `tests/ecs` on sys.path so `from fake_port import ...` works (same style as the ecs tests).

## TDD
RED: `ModuleNotFoundError: No module named 'terminal_sim.kpi'` (1 collection error).
GREEN: `9 passed in 0.04s`. Full suite: `163 passed in 11.21s`.

## KPI keys (order)
moves_per_hour_total, moves_per_hour_<STS-id> (one per STS; with the FakePort only STS-01), agv_wait_s_mean, agv_travel_s_mean, utilisation_STS, utilisation_AGV, utilisation_ASC, vessel_turnaround_s, faults, replans, energy_kwh_per_move, containers_done, containers_failed.

## Self-review / concerns
- First sample of an AGV has no previous position, so nothing is attributed (neither wait nor travel).
- Wait/travel are normalised by completed TRANSPORT jobs, per the spec; the runner must call `sample()` every 60 s (constant SAMPLE_INTERVAL_S).
- The CSV header is fixed by the first row; later rows with a different key set (e.g. a different STS count) would misalign. Not handled (YAGNI).
- `moves_per_hour_total` is the sum of the per-STS rates; a crane move on a non-STS equipment id is not counted.
