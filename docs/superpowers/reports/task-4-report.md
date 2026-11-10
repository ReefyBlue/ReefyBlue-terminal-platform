# Task 4: Yard - Report

Status: DONE

## Implemented
packages/sim/src/terminal_sim/yard.py: Yard with find_slot, is_free, occupy, release, locate, occupancy, capacity.
Storage: dict[SlotLoc, str] + reverse dict[str, SlotLoc] + per-block count (O(1) full check/occupancy).
occupy -> ValueError("SLOT_OCCUPIED"); release on empty -> KeyError; unknown block -> KeyError.

## Tests (tests/sim/test_yard.py, 5)
fills_tiers_first, occupy_twice_raises, block_full_returns_none (960 occupies), release_and_locate, unknown_block_raises_keyerror.

## TDD
RED: ModuleNotFoundError: terminal_sim.yard (collection error).
GREEN: 5 passed; full suite 46 passed.

## Files
packages/sim/src/terminal_sim/yard.py, tests/sim/test_yard.py. Commit: feat(sim): yard slot allocation and occupancy

## Self-review
Complete vs brief; no extras. Note: find_slot scans linearly (max 960 slots, fine).

## Concerns
- occupy() with a container_id already placed elsewhere would overwrite the reverse index; not in brief, not guarded.
- Tiers are not enforced to stack (can occupy tier 3 with tier 1-2 empty); brief did not require it.

## Fix report (review findings)
Changes in yard.py: added `_check_slot` (KeyError for unknown block; ValueError("SLOT_OUT_OF_RANGE") if bay/row/tier outside 1..spec) used by `occupy`, `is_free`, `release`. `occupy` also raises ValueError("CONTAINER_PLACED") if container_id already placed (checked after SLOT_OCCUPIED, before any mutation). `is_free` now raises KeyError for unknown block.
New tests: test_out_of_range_slot_rejected (5 params; occupy/is_free/release, count/find_slot unaffected), test_occupy_already_placed_container_raises (index and count stay consistent), test_is_free_unknown_block_raises_keyerror.
Command: `uv run pytest tests/sim/test_yard.py`
Output: 12 passed in 0.29s (full suite: 53 passed).
