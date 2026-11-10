# Task 16 report

Status: DONE. No defects found; no bounds loosened.

Implemented: tests/e2e/test_baseline.py (200/200 + window 20..45 + reproducibility), tests/e2e/test_scenarios_compare.py (agv4 <= base non-strict, agv2 < base strict, faults completes with faults>0, replans>0, slower than base), tests/test_architecture.py (regex on import/from lines: ecs !-> sim; sim !-> ecs/tos_stub/runner; model !-> any other terminal_*; plus a self-test that docstring mentions are ignored and real imports are caught), e2e marker in pyproject.toml, README.md (Dutch). All e2e assert timed_out == 0.0.

Tests: `uv run pytest -q` -> 193 passed in 17.35s (wall 17.6s). e2e+architecture alone: 12 tests ~5s.

Measured KPIs (seed 1):
- baseline: 200/200, 60.91 moves/h total, 30.46 STS-01, 11820 s
- agv4: 200/200, 60.61 total, 11880 s
- agv2 (baseline with n_agv=2): 200/200, 30.93 total, 23280 s
- faults: 200/200, 40.00 total (20.0 per STS), 18000 s, 19 faults, 5 replans

Self-review: no scenario yaml added (agv2 built via dataclasses.replace). Module-scoped fixtures avoid rerunning baseline. Concern: faults test additionally asserts throughput < baseline (extra, holds with large margin).
Commits: see git log (2): test commit, docs commit.
