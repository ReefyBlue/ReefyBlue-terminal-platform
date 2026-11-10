# Task 6 report

Status: DONE_WITH_CONCERNS

- New e2e `tests/e2e/test_service_small.py` (sim + gateway + ECS service + `run_once_http`): PASS, 43.9 s total (incl. startup); assertions incl. wall < 90 s.
- Default suite `uv run pytest -q`: 439 passed, 4 deselected, 257.8 s.
- `uv run pytest -m e2e_opcua -q`: 4 passed, 439 deselected, 390.9 s.
- Docs: README section "ECS als service (Plan B2a)"; spec §3 (INVALID_ORDER, `sim_time: null`).

## Product fixes (in serve.py, found by the e2e)
1. SIGTERM gave exit code -15: uvicorn 0.54 re-raises the captured signal after graceful shutdown. Added `_Server(uvicorn.Server)` in `serve.py` that clears `_captured_signals` in `handle_exit`; `main` uses `_Server(config).run()` -> exit 0.
2. `tests/ecs_service/test_app.py::test_serve_main_builds_app` patched `uvicorn.run`; updated it to patch `serve._Server` (otherwise it started a real server and hung).

## Concerns
- `_captured_signals` is a private uvicorn attribute (pinned behaviour of 0.54).
- A first full run hung (~30 min) because of fix 2 before the test was updated.

## Fix round 1
- `serve.py`: removed `_Server` subclass / private `_captured_signals`; `main` now installs `signal.signal(SIGTERM, lambda *_: sys.exit(0))` before `uvicorn.run(...)`. Docstring corrected (SIGTERM -> exit 0 via handler; SIGINT keeps KeyboardInterrupt/130).
- `test_serve_main_builds_app`: patches `uvicorn.run` again, records `signal.signal` calls, asserts a SIGTERM handler was installed (exits 0 when called) and the real handler is untouched.
- `uv run pytest tests/ecs_service -q`: 49 passed. `uv run pytest tests/e2e/test_service_small.py -m e2e_opcua -q`: 1 passed in 44.0 s (ECS returncode 0 asserted).
