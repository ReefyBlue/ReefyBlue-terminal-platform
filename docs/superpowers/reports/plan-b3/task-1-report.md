# Task 1 report: pure event codec

Implemented `event_fields` / `event_from_fields` in packages/opcua/src/terminal_opcua/codec.py; removed `event_to_json` / `event_from_json`. Empty string maps to None; missing key or unknown NewState raises ValueError (State() raises ValueError itself).

Tests: replaced event-json tests in tests/opcua/test_codec.py with the 4 brief tests (dropped the old bad-input json test).

TDD: RED = `uv run pytest tests/opcua/test_codec.py -q` -> ImportError: cannot import name 'event_fields'. GREEN = same command -> 8 passed.

Files: codec.py, test_codec.py only. Other modules still import the removed functions (expected, later tasks).

Self-review: minimal, matches brief. Concern: none.
