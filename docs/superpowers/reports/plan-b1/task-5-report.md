# Task 5 report

Status: DONE_WITH_CONCERNS

- Added ControlServer (opcua_servers.py) and serve.py (SimService, main). Emits are routed via a world.port subscriber; Equipment.emit untouched.
- nodeset.create_control_object now also creates the SimTime and SpeedFactor Double variables on Terminal/Clock (ControlType had none; Task 6 gateway should mirror them).
- Tests: tests/sim/test_serve.py (3 tests) pass; full suite 288 passed.

Measurements (12 equipment servers + control, 2 STS / 6 AGV, idle world, no clients):
- Startup: ~14 s total (~1 s CPU per server, asyncua init; gather does not help). Clients must retry connecting.
- One tick (world.run 1 s sim + sync of all servers): ~1 ms run + ~2-4 ms sync, i.e. ~4-5 ms wall.
  Idle ceiling is therefore ~200+ x; under load with events/subscribed clients it will be higher, so Task 8 should measure with the gateway attached before choosing speed_factor above ~100.

Notes: subprocess test writes stderr to a file (asyncua INFO logging fills a pipe and blocks the child); control-port poll is 45 s, not 15 s, because of startup time.

## Fix report (review round)
- PrefillYard now validates the whole batch first (slot free, no duplicate slot/container in batch or yard), then occupies; any ValueError/KeyError rolls back placed items via yard.release and returns False.
- Pacing test now speed_factor 100, bounds 25..75 after 0.5 s wall.
- SimService raises ValueError (not assert) when equipment > 12.
- New tests: test_prefill_yard_is_atomic (duplicate slot, duplicate cid, unknown block, out-of-range: all False, yard unchanged, no client exception), test_too_many_equipment_raises.
- Command: uv run pytest tests/sim/test_serve.py tests/sim/test_opcua_servers.py -q -> 10 passed in 64.49s
