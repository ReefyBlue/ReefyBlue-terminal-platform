# Task 2 report

Implemented terminal_viewer package (static placeholders, static_dir()), `_viewer_dir()` module-level in app.py
(looked up at call time in create_app), /ui StaticFiles mount (html=True) and `/` -> 307 `/ui/`, info log either way.
Wired pyprojects (root dev group, sources, pytest pythonpath; ecs_service optional extra `viewer` + source), uv.lock updated.
Architecture test: viewer in PACKAGES/ALLOWED, terminal_viewer allowed for ecs_service, test_viewer_has_no_python_logic.

TDD: RED = 4 failed (2 new UI tests, viewer sources guard, no-python-logic test) before implementation.
GREEN: tests/ecs_service/test_layout_and_ui.py + tests/test_architecture.py -> 36 passed.
Full suite `uv run pytest -q`: 539 passed, 6 deselected (278s).
ruff: fixed import order I001 in app.py (my own).

Self-review: no concerns; app.py imports terminal_viewer lazily inside the try, which the architecture regex still sees (allowed).
