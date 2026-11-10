# Task 6 report: Gateway

Status: DONE. Commit: 0e21a72 `feat(gateway): OPC UA aggregation gateway with reconnect`.

## Files
- New: `packages/gateway/pyproject.toml`, `packages/gateway/src/terminal_gateway/{__init__,mirror,gateway,serve}.py`, `tests/gateway/test_gateway.py`
- Modified: `packages/opcua/src/terminal_opcua/nodeset.py` (handlers may be coroutine functions; wrappers `await` awaitable results; applies to equipment methods and control Stop/PrefillYard), `tests/opcua/test_nodeset.py` (+1 test), `packages/sim/src/terminal_sim/layout.py` (`opcua_base_port=48400`, `opcua_host="localhost"`, from optional top-level `opcua:` block), `tests/sim/test_layout.py` (+1), `terminal.yaml` (+`opcua: {base_port: 48400, host: localhost}`), root `pyproject.toml` (dev dep, workspace source, pythonpath), `uv.lock`, `tests/test_architecture.py` (gateway in PACKAGES; a new test checks gateway imports only terminal_model/terminal_opcua/asyncua/yaml/stdlib).

## Tests
- `tests/gateway`: the 5 tests from the brief plus `test_serve_sigterm_exits_cleanly` (subprocess, no upstreams, SIGTERM -> exit 0, no traceback). About 24 s. Ran 4 times in a row, all green.
- Full suite: `uv run pytest -q` gave **300 passed in 154 s**.

## Measured mirror latency
From upstream `sync()` (State BUSY written) to a gateway client reading `State == "BUSY"`: **34–48 ms** over 5 runs (polling step 5 ms). This fits the 50 ms publishing interval.

## Design notes / deviations
- **Layout and the import rule.** `terminal_gateway` may not import `terminal_sim`, so `Gateway(layout, ...)` takes any object that has `.sts[*].equipment_id` and `.blocks[*].{block, asc_id}` (a `LayoutLike` Protocol). The tests pass the real `terminal_sim` `Layout`. `serve.py` reads `terminal.yaml` itself with `load_gateway_layout`, which picks up the ids and the `opcua` block.
- Signature: `Gateway(layout, *, port=4840, upstream_host, base_port, n_sts=2, n_agv=6, host="0.0.0.0", reconnect_s=5.0)`. The gateway endpoint is `opc.tcp://{host}:{port}/terminal/gateway` (`Gateway.endpoint`). Upstream URLs are `opc.tcp://{upstream_host}:{port}/terminal/{id}` and `.../terminal/control`. `upstream_table()` raises if there are more than 12 equipment.
- State values are the enum values (`"FAULT"`, `"IDLE"`, `"BUSY"`), not the `"Fault"`/`"Idle"` spelling in the brief.
- **Disconnect detection.** Uses asyncua 2.1's built-in supervisor with `auto_reconnect=False` and `watchdog_intervall=1 s`. Its `connection_lost_callback` sets an Event, and our own loop reconnects every 5 s. A server `stop()` is detected well within 3 s.
- **Initial Fault event.** A failed first connect also synthesizes the FAULT `STATE_CHANGED` event (EventSeq 0→1, `reason="UPSTREAM_DISCONNECTED"`, `sim_time` = last known SimTime). Test 3 needs this. The event is synthesized once per outage, not on every retry.
- **LastEvent/EventSeq ordering.** The asyncua server groups the notifications of one publish by monitored item, not by write order. With the default queue (1) it also coalesces values. So the gateway subscribes with `queuesize=100` and queues incoming LastEvent values. Each EventSeq notification takes the next queued LastEvent, then writes LastEvent first and EventSeq second. I did not use "read upstream LastEvent on EventSeq change": that read can already return a newer event when two events land in one publish. Known edge case: two byte-identical consecutive LastEvent JSONs produce only one LastEvent notification, so the pairing shifts by one event. Only exact duplicates (same kind, time, job and reason) trigger it.
- **EventSeq after reconnect (deviation from "write max(upstream, last)").** With plain max(), events get lost in the most common case, a gateway that starts before the sim. The gateway synthesizes Fault at seq 1, the fresh upstream starts at 0, and the upstream's first real event (seq 1) would be written as max(1, 1) = 1. The value does not change, so no datachange fires and the event is never seen. Instead, on resync the gateway computes `offset = max(0, gateway_seq - upstream_seq)` and logs a WARNING (once per reconnect). From then on gateway seq = upstream + offset. The offset is 0 until a Fault has been synthesized, so the gateway does not renumber in normal operation. Measured in test 3: Fault → 1, upstream resyncs at 0 (gateway stays 1, LastEvent switches to the upstream's current event without a seq bump), the next upstream event (upstream seq 1) shows on the gateway as 2. If the upstream counter went backwards without the gateway noticing a loss, the gateway holds its value and logs a WARNING.
- Resync: on every (re)connect, all variables are read with `read_values` and written, and then a subscription is created. Its initial notifications rewrite the same values, which triggers no datachange.
- A method call to an absent upstream returns `False` (WARNING log). Stop has no return value. The control mirror has no State of its own, so while it is down the Clock values stay at their last value. `Gateway.last_sim_time` is pushed to every mirror through `set_sim_time`.
- Log noise: only the first connect failure per mirror logs at WARNING; later retries log at DEBUG. `disconnect()` is skipped when `connect()` failed, because asyncua cleans up its own socket.
- Ruff: the new files are import-sorted. The remaining BLE001/S110 hits are intentional broad catches, and the repo already has 23 pre-existing ruff findings, so ruff is not enforced.

## Fix report (review findings 1–3 + minors)

### What changed
The rule is now: **LastEvent is never written without bumping EventSeq.**

`packages/gateway/src/terminal_gateway/mirror.py`:
- `_write_event` asserts `seq > self._seq` and always writes LastEvent, then EventSeq.
- New state tracks the last delivered upstream event: `_delivered_up_seq` and `_delivered_le`.
- **Resync, finding 1.** If upstream seq != `_delivered_up_seq` AND upstream LastEvent != `_delivered_le`, the gateway delivers the upstream's latest event at `new_seq = max(self._seq + 1, up_seq)` and sets `offset = new_seq - up_seq`. The `max` keeps a first connect without a prior Fault verbatim (offset 0), so nothing is renumbered in normal operation. Otherwise it writes no event and sets `offset = self._seq - up_seq`, so the next upstream event maps to `self._seq + 1`. A WARNING is logged whenever the offset is non-zero.
- **Resync, finding 2.** LastEvent is no longer rewritten on resync when nothing happened.
- **Live path.** Each EventSeq notification is paired FIFO with a queued LastEvent. The pair is skipped if its LastEvent equals `_delivered_le` and its seq is ≤ `_delivered_up_seq`, which covers the initial notifications after a resync. Otherwise `_deliver_upstream` maps `seq + offset`. If that is not above the gateway seq, it forces `self._seq + 1` and adjusts the offset (WARNING), so an event is never swallowed.
- Both mirrors get a no-op async `status_change_notification`.

`gateway.py`: `start()` creates the mirrors in `_create_mirrors()`. On any failure it calls `stop()`, which stops the mirrors and the server, then re-raises.

`Gateway(..., reconnect_s=...)` was already a parameter. The proxy tests use 1.0 s.

### Covering tests (`tests/gateway/test_gateway.py`, helper `tests/gateway/tcp_proxy.py`)
`EventConsumer` subscribes to LastEvent and EventSeq with queuesize 100 and pairs them FIFO, as the ECS port will.
- `test_three_events_in_one_sync_delivered_in_order`: J1/J2/J3 in one `sync()` arrive as exactly `(n+1,J1),(n+2,J2),(n+3,J3)`.
- `test_blip_with_one_event_delivers_it`: the TCP proxy is cut while the upstream keeps running; J6 is emitted during the outage. The consumer sees Fault, then J6 with seq > Fault seq, then J7/J8 at J6+1 and J6+2.
- `test_blip_without_events_writes_no_last_event`: a blip with 0 events produces exactly 1 LastEvent write (the Fault). The next J1/J2 arrive at Fault+1 and Fault+2, and the pairing queue is empty afterwards.
- `test_start_failure_stops_server`: a mirror-creation failure raises and the gateway port no longer accepts connections.

Run with the previous mirror.py, both blip tests FAIL; with the fix, all pass.

### Command / output
`uv run pytest tests/gateway tests/opcua -q`, run 3× in a row: `25 passed in 56.97s`, `25 passed in 56.48s`, `25 passed in 57.89s`.
Mirror latency is unchanged: 47 ms.

## Fix report 2 (re-review: empty payload after a fresh upstream restart)

### What changed
- `mirror.py`: new `_is_event(payload)` check. It is true only for a non-empty string that `event_from_json` decodes.
  - Resync: delivers only when `up_seq != _delivered_up_seq` and `up_le != _delivered_le` and `_is_event(up_le)`. A fresh upstream (EventSeq 0, LastEvent "") now takes the else-branch, which only shifts the offset; no event is written.
  - Live path: a pair whose LastEvent does not decode is skipped with a WARNING. The upstream always writes a real LastEvent before EventSeq, so this is defensive only.
- `gateway.py`: a failed `start()` now also clears `self.mirrors` and sets `self.control = None`, so a retry does not duplicate entries.

### Covering tests
- `test_fresh_upstream_restart_emits_no_empty_event`: J1 is delivered, then the `EquipmentServer` is stopped (the consumer sees the Fault). A fresh server is started on the same port (EventSeq 0, LastEvent ""). The test asserts that the consumer saw exactly 2 LastEvent writes (J1 and the Fault) and no undecodable payload. J2/J3 then arrive at Fault+1 and Fault+2, and the pairing queue is empty. With the `_is_event` guard removed, this test FAILS.
- `test_start_failure_stops_server` now fails on the second mirror's start (the first was created) and asserts `mirrors == {}` and `control is None`.

### Command / output
`uv run pytest tests/gateway -q`, 2 runs: `11 passed in 44.87s`, `11 passed in 44.18s`.
