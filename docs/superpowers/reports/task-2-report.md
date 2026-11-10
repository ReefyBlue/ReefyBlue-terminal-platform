# Task 2 report: EquipmentPort-interface

Status: DONE

## Implemented
packages/model/src/terminal_model/port.py: EquipmentSnapshot (frozen), EventKind (StrEnum), EquipmentEvent (frozen), EquipmentPort (@runtime_checkable Protocol, exactly seven methods). `__init__.py` is empty (no re-exports), so unchanged.

## Tests
tests/model/test_port.py::test_port_is_runtime_checkable: full class passes isinstance; class without `now` does not.

## TDD evidence
RED: `uv run pytest tests/model/test_port.py` -> ModuleNotFoundError: No module named 'terminal_model.port' (collection error).
GREEN: same command -> 1 passed. Full suite `uv run pytest` -> 31 passed.

## Files
- packages/model/src/terminal_model/port.py
- tests/model/test_port.py

## Self-review
Matches brief; nothing extra. Note: runtime_checkable isinstance checks method presence only, not signatures.

## Concerns
None.
