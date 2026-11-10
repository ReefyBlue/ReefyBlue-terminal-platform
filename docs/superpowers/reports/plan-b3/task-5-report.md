# Task 5 report: Gateway stuurt events door

**Status:** DONE_WITH_CONCERNS
**Commit:** d037741 `feat(gateway): forward upstream OPC UA events with own Seq` (branch feat/plan-b3-events)

## What
- `packages/gateway/src/terminal_gateway/mirror.py` (418 -> 374 lines):
  - `start` creates `self._emitter = EventEmitter.create(server, types.events, obj, equipment_id)`.
  - `_on_connected`: resolves `up_types = resolve_event_types(client)`, writes every snapshot variable under `_write_lock`, emits the recovery event (if `_fault_announced` and upstream State is not FAULT; an unknown State logs a warning and sends no recovery), then creates ONE subscription (handler = the mirror) with datachange on the snapshot vars plus `subscribe_terminal_events(client, sub, up_types)`.
  - New `event_notification(event)`: `event_from_ua`; on ValueError it logs a warning and skips the event. Events for another `EquipmentId` are ignored. A gap logs `"%s: upstream Seq gap %d -> %d"`, then `_up_seq = seq` and `await self._emit(ev)`. Re-emit failures are logged with `log.exception`.
  - New `_emit(ev)` = `await self._emitter.emit(ev)`. `_on_lost`: State=FAULT + FAULT event (`UPSTREAM_DISCONNECTED`), once per outage, under `_write_lock`.
  - Removed: `_is_event`, `_write_event`, `_deliver_upstream`, `_le_queue`, `_offset`, `_delivered_up_seq`, `_delivered_le`, `_last_upstream_le`, `_seq`, the `collections` import, the `event_to_json`/`event_from_json` imports, and the local `EVENT_QUEUE` (it now lives in `terminal_opcua.events` and nothing else imported it from the mirror). The datachange subscription now uses the default queue size, because the snapshot vars only need their last value. The docstring now describes the new invariant.
- `gateway.py`: no change needed. `_create_mirrors` already passes `types` from `register_types`, and `types.events` comes from there.
- `tests/gateway/test_gateway.py`: `EventConsumer` is replaced by `EventSink` (`rig.sink`, attached in `_start_connected`, detached in teardown). New helpers: `_sts(r)` (only STS-01 events; the ASC mirrors without an upstream emit their own FAULTs on the same gateway Server node), `_jobs`, `_emit_jobs`, and `_linked` (waits until `mirror.connected`). All tests named in the brief were rewritten, renamed or added. The non-event tests were left alone.

## TDD
- RED: `uv run pytest tests/gateway -q -x` gave a collection error, `ImportError: cannot import name 'event_from_json'` in mirror.py.
- First GREEN attempt: 6 failed / 9 passed. The cause was a race in the rig, not the mirror. `State == IDLE` (or the recovery event) appears **before** `_on_connected` has created the subscriptions, so events emitted right after were lost and `Reset` was "not forwarded". The fix was to also wait for `_linked()` (`mirror.connected`) in `_start_connected`, `_blip` and after a reconnect.
- GREEN: `uv run pytest tests/gateway -q`: 15 passed (run 3x, all green, ~62 s).
- `uv run pytest tests/gateway tests/sim tests/opcua/test_events.py tests/opcua/test_nodeset.py tests/opcua/test_codec.py -q`: 155 passed. On the first run it was 1 failed / 154 passed: `tests/sim/test_serve.py::test_sim_starts_paused_until_start` gave an OSError. That test passes alone, on the baseline, and on the rerun. It is a sim test that does not use the gateway, so it looks like the existing `_free_base_port` collision (base..base+12).
- Output: only the known asyncua WARNINGs (Revised values / session timeout / no encrypting policy) plus the expected "upstream unavailable" warnings in captured logs on failure. Ruff: no new findings. The BLE001/S110 findings in mirror.py were already there.

## Self-review
- Brief/spec §5/§7: subscribing to the upstream Server object's events, gap warning, re-emitting with the mirror's own Seq, ignoring other EquipmentIds, a single FAULT per outage, recovery skipped for an upstream FAULT, unknown State gives a warning: all covered by tests (`test_event_for_other_equipment_ignored` calls `event_notification` directly with a `SimpleNamespace`, as the brief asks).
- Concurrency: Seq order is guaranteed by `EventEmitter`'s lock. `_write_lock` still guards the snapshot writes in `_on_connected` against FAULT/recovery. Datachange writes stay unlocked, as before. The recovery event always comes before post-reconnect upstream events, because the subscription is only created after the recovery emit.
- Real behaviour: the blip tests go through `TcpProxy`; the restart test really stops and starts the upstream.

## Concerns
1. **Deviation from the brief and controller: `_up_seq` is NOT reset to 0 on reconnect.** The two requirements contradict each other. With a reset plus the `if self._up_seq and ...` guard, a gap across an outage can never be detected, so `test_blip_with_one_event_logs_gap` (and spec §5, "zichtbaar als Seq-gat in de log") would be impossible. Dropping the guard instead would give false gap warnings after every blip. Without the reset, everything holds: a restarted upstream (Seq back to 1, so `seq <= _up_seq`) never warns, and there is no "not ahead" warning left in the code at all. Side effect: after an upstream restart, a gap is only reported once the new Seq exceeds the old `_up_seq + 1` (it can then say e.g. "gap 5 -> 7" across the restart), which is harmless. If the reset is wanted after all, the gap test has to go or change.
2. `test_blip_with_one_event_logs_gap` emits J5 **before** the cut (upstream Seq 1), otherwise the guard `_up_seq == 0` prevents any warning. Exact expected log line: `STS-01: upstream Seq gap 1 -> 3`.
3. `test_gateway_before_sim_recovers`: the FAULT for the failed first connect usually happens before the sink attaches (connection refused is immediate). The test therefore checks `_emitter.seq == 1` before the sim starts, and accepts the event list `[FAULT(1), IDLE(2)]` or `[IDLE(2)]`, followed by BUSY at seq 3.
4. Inherent window: upstream events between `read_values` and the subscription in `_on_connected` are lost (the same as during an outage, but without a gap warning if it is the first event after the previous `_up_seq`, which is rare). Tests avoid it with `_linked()`. The ECS port (Task 4) can consider the gateway "ready" when the snapshot is there, which is slightly earlier than events actually flowing.
5. Flaky `tests/sim/test_serve.py` port selection (existing, not touched).
