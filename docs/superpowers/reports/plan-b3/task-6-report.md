# Task 6 report

- Default suite: `uv run pytest -q` -> 354 passed, 3 deselected in 264.83s (4:24). Incl. test_architecture and ECS hash.
- e2e: `uv run pytest -m e2e_opcua -q -s` -> 3 passed in 355.36s (5:55).
- Equivalence (small scenario at x25, test_small_scenario_gap_shrinks_with_lower_speed): containers_done 20/20, failed 0/0, stuck 0/0, replans 0/0, faults 0/0, sim_elapsed 2520/2520, moves_per_hour_total 28.5714/28.5714 (exactly equal), vessel_turnaround_s 2235.2 vs 2267.2 (within 120 s tolerance), energy 8.016/8.016. The x100 baseline table scrolled off the captured tail (not recorded); that test passed with its asserts (counts exact, moves/h within 10 %, turnaround within 900 s).
- grep LastEvent|EventSeq|event_to_json|event_from_json: hits only in B1 spec (historical), B3 spec (describes replacement), and tests/opcua/test_nodeset.py lines 55 and 72 (negative assertions that these names are gone; intentional). README had no mention of LastEvent/EventSeq.
- Docs: README gets new subsection "Equipment-events (OPC UA Events, Plan B3)" under OPC UA-modus: EventTypes table, Seq semantics, outage-loss note, asyncua example (resolve_event_types + subscribe_terminal_events + event_from_ua). B1 spec s4: one line pointing to B3 spec.
- Concerns: none blocking. Asyncua "Revised values returned differ" warnings not chased. README example not executed (no server run), checked against signatures in events.py/port.py. The added B1 line sits after the Variabelen bullet, separated by blank lines.
