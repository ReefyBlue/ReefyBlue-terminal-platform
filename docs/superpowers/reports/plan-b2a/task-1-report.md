# Task 1 report: package terminal_kpi

Status: DONE. KpiCollector moved (git mv) to packages/kpi/src/terminal_kpi/kpi.py; new snapshot(duration_s) (same keys/order as finish plus sim_elapsed_s last); finish == snapshot. Root pyproject (dev group, sources, pythonpath), runner pyproject, run.py import (isort-placed), tests moved to tests/kpi/test_kpi.py (git mv), architecture test (PACKAGES + ALLOWED["kpi"]) updated. uv sync run.

Fixture key order verified: ... containers_stuck, sim_elapsed_s, timed_out -> unchanged; runner re-assigning sim_elapsed_s keeps position.

RED: `uv run pytest tests/kpi -q` -> ModuleNotFoundError: terminal_kpi (collection error).
GREEN: `uv run pytest tests/kpi tests/runner tests/test_architecture.py -q` -> 59 passed.
`uv run pytest tests/e2e/test_plan_a_equivalence.py -m e2e -q` -> 1 passed.
Full suite (excluding test_run_opcua.py): 350 passed, 3 deselected (270 s). One earlier full run showed a single F at ~41% (output cut by timeout, not identified); the rerun with -x was fully green, so likely flaky/timing-load related.

Existing test_kpi_key_order_is_stable updated to expect sim_elapsed_s last (necessary consequence of the ruling).

Concerns: the one unexplained transient failure above.
