# Plan B1 — final-review fix wave report

Branch feat/plan-b1-opcua, commits 9aeb6eb..11a243d. ECS sources untouched (hash test green).

## I1 — equipment stays excluded after a gateway↔server reconnect — 2a966e6
- `mirror.py`: new `RECOVERY_REASON = "UPSTREAM_RECONNECTED"` and `EquipmentMirror._announce_recovery`.
  In `_on_connected`, when a Fault was announced, after the (optional) delivery of the upstream's latest
  event, writes `STATE_CHANGED(new_state=State(upstream State), reason=UPSTREAM_RECONNECTED,
  sim_time=last known SimTime)` at `self._seq + 1` via `_write_event` (LastEvent then EventSeq), then
  re-derives `self._offset = self._seq - up_seq` so the next upstream event maps above it.
- Deviation (deliberate): no recovery event when the upstream State is itself FAULT (the device should stay
  excluded; a second FAULT would also inflate the `faults` KPI and replan nothing). Unknown State → warning, no event.
- Tests: `tests/gateway/test_gateway.py::test_blip_on_idle_upstream_reports_recovery_to_idle` (new; Fault then
  recovery IDLE at fault_seq+1, sim_time 42.0), `::test_blip_on_faulted_upstream_sends_no_recovery` (new);
  existing blip / gateway-before-sim / fresh-restart tests updated for the extra event and seq.
  `tests/opcua/test_port_reconnect_ecs.py::test_dispatcher_uses_agv_again_after_link_blip` (new; TcpProxy in
  front of AGV-01, Dispatcher on OpcUaEquipmentPort, n_agv=1: after the blip `status()["equipment"]["AGV-01"]`
  is IDLE and a DISCHARGE order gets a TRANSPORT on AGV-01). Verified it fails with the old mirror.py.
- README (OPC UA-modus): bullet on FAULT on link loss / STATE_CHANGED with UPSTREAM_RECONNECTED on recovery,
  plus "comm loss as equipment fault is a B2/B3 decision".

## I2 — failed run never stops the sim — 83360b6
- `run.py`: `stopped` flag; the `finally` calls `_stop_quietly(port, gateway)` unless the success path already
  sent Stop — covers failures before Start (paused sim) and after Start. Errors from `stop_sim` are logged
  (warning) and swallowed; the original exception propagates.
- Tests (`tests/runner/test_run_opcua.py`, `FakeOpcPort` backed by an in-process World):
  `test_failure_after_start_stops_sim` (Dispatcher.tick raises after Start → calls end start, stop, close),
  `test_failure_before_start_stops_paused_sim` (PrefillYard rejected → stop, close),
  `test_stop_errors_on_failed_run_are_swallowed`.

## I3 — fleet mismatch silently shifts ports — 4f27d90
- `run.py`: `_check_fleet` after `_wait_ready`: per-kind counts from `port.snapshot(eid).kind` vs
  `n_sts`, `n_agv`, `len(layout.blocks)`; mismatch → error log + stderr naming `--n-sts X --n-agv Y`,
  `SystemExit(1)` (the I2 finally then sends Stop).
- Tests: `test_fleet_mismatch_exits_1` (fake fleet with 3 AGVs vs scenario 6), `test_matching_fleet_passes_check`.
- README: sentence in the "Volgorde" paragraph.

## Minors — 11a243d
- `tests/e2e/opcua_procs.py`: compares with `State.FAULT.value`; device count = n_sts + n_agv + layout blocks.
- `tests/e2e/test_opcua_equivalence.py`: ×25 moves/h band 5 % (turnaround 120 s unchanged).
- `port.py`: `CALL_TIMEOUT_S = 3.0`; `close()` creates the fresh event queue after the loop thread stopped
  (and still on the never-connected path).
- README: `agv_wait_tp_s_mean` row (TP arrival → next TRANSPORT assignment), drain "≥ 10 ms" (2×),
  suite "≈ 4 minuten", ×25 band 5 %, second timing effect (SimTime and events over separate connections; the
  B2 step-on-demand clock removes it).

## Test runs (after all fixes)
- `uv run pytest -q`: 344 passed, 3 deselected in 253.22 s (4 m 13 s wall).
- `uv run pytest -m e2e_opcua -q`: 3 passed, 344 deselected in 369.22 s (6 m 09 s wall).
  ×25 small scenario: counts equal; vessel_turnaround 2235 s in-process vs 2260 s OPC UA.

## Residuals
- Recovery event is skipped when the upstream is FAULT (deviation from the literal brief, reasoned above).
- A BUSY upstream at reconnect yields STATE_CHANGED→BUSY, which does not clear the ECS exclusion; the upstream's
  own later STATE_CHANGED→IDLE does. Acceptable (the ECS replanned that device's job on the Fault).
- Deferred minors from earlier tasks (ledger) unchanged, e.g. orphaned sim process if gateway spawn raises in
  the e2e helper.
