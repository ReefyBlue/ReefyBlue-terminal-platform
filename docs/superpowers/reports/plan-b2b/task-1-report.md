# Task 1 report: GET /layout

Implemented: schemas PositionOut/StsOut/BlockOut/LayoutOut; core.py stores `self.layout` and LayoutLike/_BlockLike protocols extended (only read in the route, __init__ unchanged); app.py `GET /layout` (no guarded/503).
Tests: tests/ecs_service/test_layout_and_ui.py (2 tests).
RED: `uv run pytest tests/ecs_service/test_layout_and_ui.py -q` -> 2 failed (404 == 200).
GREEN: same command -> 2 passed. Full `uv run pytest -q` -> 534 passed, 6 deselected.
Files: schemas.py, core.py, app.py, test_layout_and_ui.py.
Self-review: no concerns; existing SimpleNamespace fixtures untouched.
