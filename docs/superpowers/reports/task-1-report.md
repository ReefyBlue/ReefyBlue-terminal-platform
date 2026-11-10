# Task 1 report

## Implemented
uv workspace (root + 4 hatchling packages with src layout and empty __init__), terminal_model: types.py, locations.py, logging.py; .gitignore; uv.lock committed.

## Tests
`uv run pytest` -> 30 passed (types, locations, logging).

## TDD evidence
RED: `uv run pytest tests/model -v` -> 3 collection errors, `ModuleNotFoundError: No module named 'terminal_model.types'`.
GREEN: `uv run pytest -v` -> 28 passed; after hardening (fullmatch, ASCII digits) 30 passed.

## Files
pyproject.toml, .gitignore, uv.lock, packages/{model,sim,ecs,tos_stub}/pyproject.toml + src/<pkg>/__init__.py, packages/model/src/terminal_model/{types,locations,logging}.py, tests/model/test_{types,locations,logging}.py

## Self-review
- Parsing uses fullmatch + [0-9] so trailing newline and non-ASCII digits are rejected (tests added).
- format_location validates too (raises ValueError).
- Added extras beyond brief: test_logging.py, JSON formatter includes exc info, tests for valid container lengths and Job defaults.
- uv emits a deprecation warning about UV_NATIVE_TLS env var (environment, not project).

## Concerns
None. Note: salabim not yet imported anywhere; sync resolved fine over network.

## Fix report (review finding: member deps not installed)
Changed: root pyproject.toml dev group now lists terminal-model, terminal-sim, terminal-ecs, terminal-tos-stub, with `[tool.uv.sources]` workspace references; uv.lock updated.
Command: `uv run python -c "import salabim, yaml; print('ok', salabim.__version__, yaml.__version__)"`
Output: `ok 26.0.9 6.0.3`
Command: `uv run pytest -q` -> `30 passed in 0.03s`
