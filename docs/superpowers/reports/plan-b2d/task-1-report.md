# Task 1 report
Added State.OFFLINE (types.py), LinkReason StrEnum (port.py), model test test_state_offline_and_link_reason, faults reference fixture (generated with pre-B2d code, ~2s) and tests/e2e/test_faults_equivalence.py (marker e2e, five keys).
RED: model test failed with ImportError (LinkReason); equivalence PASS on unchanged code.
GREEN: `pytest tests/model tests/e2e/test_faults_equivalence.py tests/test_architecture.py` -> 62 passed.
Fixture values: containers_done=200.0, containers_failed=0.0, replans=15.0, faults=17.0, moves_per_hour_total=43.956043956043956
Files: packages/model/src/terminal_model/{types,port}.py, tests/model/test_types.py, tests/e2e/test_faults_equivalence.py, tests/e2e/fixtures/faults_pre_b2d_seed1.json
Concerns: none. Fixture generation printed "replan ..." lines to stdout (run_once logs), not in the JSON.
