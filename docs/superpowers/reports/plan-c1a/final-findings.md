# Final review findings — one fix wave

Important 1 — spec §4 rack/lane overlap validation (implement, don't defer): in `validate`, per transfer zone a 1-D interval check on rack x (half-length by size: 20'→3.029, 40'→6.096, 45'→6.858 m; touching allowed), per STS on lane x ± width/2 (touching allowed), with messages naming both ids; tests `test_overlapping_racks_rejected`, `test_overlapping_lanes_rejected`, and both shipped layouts must still validate.
Important 2 — `tests/e2e/test_mv_small_baseline.py`: never regenerate silently — fail with a clear message when the fixture is missing; write it only when env `WRITE_FIXTURES=1` (match how the legacy fixture tests do it; if they regenerate on absence too, change both to the flag).
Important 3 — `layout.py` validate: duplicate berth ids and duplicate drive-lane ids rejected (`_dupes`), one test each.
Minor 1 — reword the `layout.py` comment citing "the brief" to state the rule itself.
Minor 2 — coerce `OpcUa` (`int(base_port)`, `str(host)`); parse `reefer` with a strict bool (`True/False` or "true"/"false" case-insensitive; anything else → LayoutError).
Minor 3 — `TerminalOut` drift guard: `extra="forbid"` on all TerminalOut sub-models AND a test asserting the set of dataclass field names equals the pydantic `model_fields` for each pair.
Minor 4 — positive-dimension checks (bays, rows, tiers > 0; lane len/width > 0; quay length > 0; zone polygon ≥ 3 points).
Minor 5 — spec §1 criterion 1: replace "`terminal.yaml` beschrijft…" with "`terminal_mv*.yaml` beschrijven… (`terminal.yaml` blijft de legacy v1-layout)".
Minor 6 — `test_overlapping_blocks_rejected`: assert the exact message `blocks 'A' and 'B' overlap`.
Minor 7 — gateway logs `control_port` at startup (one `log.info`).

Covering tests: `uv run pytest tests/model tests/sim tests/gateway tests/ecs_service tests/e2e/test_mv_small_baseline.py tests/e2e/test_baseline.py -q`, then `uv run pytest -q`. Commit with trailer lines:
Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011Q6oL3tzJGQycNuMVuzTtk
