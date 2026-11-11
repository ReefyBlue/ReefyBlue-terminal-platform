# Task 5 report
Status: DONE. Commit 4f8cb41.
- core.py: auto_release ctor arg -> Dispatcher; alarms(); publish_alarms() diff on (kind, equipment_id, job_id); release()/resolve_job() wrappers (RuntimeError when not connected, other errors propagate) followed by publish_alarms()+publish_status(); publish_alarms called after step ticks, submit, on_link_lost, connect_once.
- Tests added: link blip, release/resolve, unknown-outcome resolve, diff idempotence, auto_release passthrough, not-connected. test_app.py ws link test adjusted to tolerate the new alarm messages.
- uv run pytest tests/ecs_service -q: 62 passed.
- Concern: blip test's "order advances" check is weak (open == 1, replans == 0 after complete).

## Fix round 1
- core.py step(): publish_alarms() now also runs right after drain_events()/_check_time, before the tick loop (in-loop call kept).
- New test test_core_step_publishes_alarms_from_drained_events (FAULT + step, no tick boundary -> alarm raised).
- Blip test: core.step() after complete; asserts a new assignment with a different job id on the second port.
- 63 passed.
