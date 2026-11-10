# Task 13 report: TOS-stub

Status: DONE. Commit 29e7edc (feat(tos_stub): work order generator and slot allocator).

## Implemented
- packages/tos_stub/src/terminal_tos_stub/allocator.py: SimpleSlotAllocator (own issued-slot sets per block, Yard.find_slot fill order, exclude, spill A-D, None when full, release, KeyError on unknown block).
- generator.py: generate_workorders (rng accepted but NOT consumed; documented) and prefill.
- pyproject: added terminal-ecs, terminal-sim deps + workspace sources; uv lock/sync done (uv.lock updated). __init__.py left empty.

## Tests (10 in tests/tos_stub)
Generator: counts/roundrobin, deterministic, mixed shares. Allocator: fill order equals Yard, spill to B after 960, None when all full, exclude, release, unknown block, prefill with real Yard.

## TDD
RED (before implementation):
E   ModuleNotFoundError: No module named 'terminal_tos_stub.allocator'
=========================== short test summary info ============================
ERROR tests/tos_stub/test_allocator.py
ERROR tests/tos_stub/test_generator.py
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
2 errors in 0.09s

GREEN: tests/tos_stub: 10 passed. Full suite: 154 passed.

## Self-review
Complete vs brief; no extras. Pristine output. Allocator scans O(capacity) per call (max 960 slots x 4 blocks), fine for 200 orders.

## Concerns
- uv.lock changed (expected from new deps).
- exclude of a non-issued slot is simply skipped, not issued.
