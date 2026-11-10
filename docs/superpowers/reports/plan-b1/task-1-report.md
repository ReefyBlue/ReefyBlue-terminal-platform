# Task 1 report: terminal_opcua package + codecs

Status: DONE. Commit 1b48b58 `feat(opcua): package scaffold and JSON codecs` (branch feat/plan-b1-opcua).

## Implemented
- packages/opcua (dist `terminal-opcua`, import `terminal_opcua`; hatchling; deps terminal-model workspace + asyncua>=2.1).
- codec.py: job_to_json/job_from_json, event_to_json/event_from_json, snapshot_extra_to_json/extra_from_json. asdict + json; StrEnum serialises as its string value; None -> null.
- from_json: missing field (KeyError), bad enum, invalid JSON or non-object -> ValueError.
- Root pyproject: pythonpath + dev-group + workspace source; uv.lock updated; uv sync ok. asyncua 2.1.0 installed.

## Tests (tests/opcua/test_codec.py, 6)
roundtrip job (deadline None, RUNNING; plus deadline float), event (new_state FAULT, reason None), missing field, bad enum, bad event input, nested extra.
RED: ModuleNotFoundError terminal_opcua. GREEN: 6 passed. Full suite: 260 passed in 43.33s.

## Self-review / concerns
- from_json requires all keys (incl. priority/deadline/status, job_id/reason/new_state), strict by design as to_json always emits them.
- tests/test_architecture.py PACKAGES list not extended to opcua (out of brief scope); may be wanted later.
- Event kind in test uses first EventKind member (did not hardcode a name).
