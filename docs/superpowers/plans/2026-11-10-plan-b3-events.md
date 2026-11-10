# Plan B3 stap 1 — Echte OPC UA Events: implementatieplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Equipment-events reizen als echte OPC UA Events (eigen EventTypes) van sim-server via gateway naar de ECS-port; `LastEvent`/`EventSeq` en de FIFO-koppellogica verdwijnen.

**Architecture:** Nieuwe module `terminal_opcua/events.py` registreert de EventTypes, zendt events uit (één `EventGenerator` per type, emitting node = `Server`, `SourceNode` = equipment-object) en decodeert ze client-side. Sim-servers, gateway-mirrors en de port gebruiken dezelfde `EventEmitter`/`event_from_ua`; abonnees luisteren op `client.nodes.server` met `evtypes=[types.base]`.

**Tech Stack:** Python 3.13, asyncua 2.1.0, salabim, pytest-asyncio (auto), uv workspace.

**Spec:** `docs/superpowers/specs/2026-11-10-plan-b3-events-design.md`

## Global Constraints

- Pakketafhankelijkheden onveranderd (architectuurtest): opcua→model; gateway→model+opcua; sim→model+opcua. `terminal_ecs` ongewijzigd (hash-fixture `tests/fixtures/ecs_sources.sha256`).
- `EquipmentPort`-protocol en `OpcUaEquipmentPort`'s publieke API blijven identiek.
- Salabim-omgevingen altijd `Environment(trace=False, yieldless=False)`.
- Methode-wrappers op asyncua blijven `async def`.
- asyncua levert events alleen aan abonnees op de emitting node → emitting node is altijd `ua.ObjectIds.Server`.
- `Seq` per apparaat: start op 1 na start van de uitzendende server, strikt oplopend; uitsluitend voor gapdetectie.
- Commits per taak; test-commando's: `uv run pytest <pad> -q` (default suite sluit `e2e_opcua` uit).

## Review Focus

1. Twee events in één `sync()` (bijv. JOB_COMPLETED direct gevolgd door STATE_CHANGED) moeten in creatievolgorde bij de port aankomen — Task 4 `test_two_events_one_sync_in_order`.
2. Een event met lege `JobId`/`Reason`/`NewState` moet als `None` landen, nooit als `""` — Task 1 `test_event_fields_roundtrip_none_fields`.
3. Gateway-herverbinding terwijl upstream BUSY is: recovery-event moet `NewState=BUSY` dragen, geen IDLE — Task 5 `test_blip_on_busy_upstream_reports_busy`.
4. Een upstream dat opnieuw start (Seq terug naar 1) mag bij de gateway geen "not ahead"-fout of verloren events opleveren — Task 5 `test_fresh_upstream_restart_keeps_gateway_seq_monotonic`.
5. Event van een onbekend EventType op het gateway-`Server`-object (bijv. asyncua's eigen `SystemEvent`) mag de port niet laten crashen — Task 4 `test_unknown_event_type_ignored`.

---

### Task 1: Pure event-codec

**Files:**
- Modify: `packages/opcua/src/terminal_opcua/codec.py` (verwijder `event_to_json`, `event_from_json`)
- Test: `tests/opcua/test_codec.py`

**Interfaces:**
- Produces: `event_fields(ev: EquipmentEvent) -> dict[str, object]` met sleutels `EquipmentId` (str), `SimTime` (float), `JobId` (str, `""` voor None), `Reason` (str, `""` voor None), `NewState` (str, `""` voor None). Geen `Seq`/`kind` (die zet de emitter).
- Produces: `event_from_fields(kind: EventKind, fields: Mapping[str, object]) -> EquipmentEvent`; `ValueError` bij ontbrekende sleutel of onbekende `NewState`; `""` → `None`.

- [ ] **Step 1: Vervang de event-json-tests in `tests/opcua/test_codec.py`**

```python
def test_event_fields_roundtrip():
    ev = EquipmentEvent(EventKind.STATE_CHANGED, "AGV-01", 12.5, new_state=State.FAULT, reason="X")
    f = event_fields(ev)
    assert f == {"EquipmentId": "AGV-01", "SimTime": 12.5, "JobId": "", "Reason": "X", "NewState": "FAULT"}
    assert event_from_fields(EventKind.STATE_CHANGED, f) == ev

def test_event_fields_roundtrip_none_fields():
    ev = EquipmentEvent(EventKind.JOB_COMPLETED, "STS-01", 1.0, job_id="J1")
    back = event_from_fields(EventKind.JOB_COMPLETED, event_fields(ev))
    assert back == ev and back.reason is None and back.new_state is None

def test_event_from_fields_missing_raises():
    with pytest.raises(ValueError):
        event_from_fields(EventKind.JOB_FAILED, {"EquipmentId": "A"})

def test_event_from_fields_bad_state_raises():
    with pytest.raises(ValueError):
        event_from_fields(EventKind.STATE_CHANGED, {**event_fields(EquipmentEvent(EventKind.STATE_CHANGED, "A", 0.0)), "NewState": "WEIRD"})
```

- [ ] **Step 2: Run** `uv run pytest tests/opcua/test_codec.py -q` → FAIL (ImportError).
- [ ] **Step 3: Implementeer `event_fields` en `event_from_fields` in `codec.py`; verwijder `event_to_json`/`event_from_json`.**
- [ ] **Step 4: Run** `uv run pytest tests/opcua/test_codec.py -q` → PASS. (Andere suites falen nu op de verwijderde functies; dat lossen Tasks 2–5 op.)
- [ ] **Step 5: Commit** `feat(opcua): pure event field codec replaces event JSON`

### Task 2: EventTypes, EventEmitter en decoder (`events.py`) + nodeset-opschoning

**Files:**
- Create: `packages/opcua/src/terminal_opcua/events.py`
- Modify: `packages/opcua/src/terminal_opcua/nodeset.py` (`EQUIPMENT_VARS`, `VAR_TYPES`, `Types`, `register_types`; verwijder `EventEmitter`, `emit_event`, `_EMITTERS`, import `event_to_json`)
- Create: `tests/opcua/event_sink.py` (testhulp, hergebruikt door Tasks 3–5)
- Test: `tests/opcua/test_nodeset.py`, nieuw `tests/opcua/test_events.py`

**Interfaces:**
- Consumes: Task 1 codec.
- Produces in `events.py`:
  - `@dataclass(frozen=True) class EventTypes: base: ua.NodeId; state_changed: ua.NodeId; job_completed: ua.NodeId; job_failed: ua.NodeId` met `def kind_of(self, event_type: ua.NodeId) -> EventKind` (`ValueError` bij onbekend).
  - `EVENT_FIELDS: tuple[tuple[str, ua.VariantType], ...]` = `EquipmentId` String, `Seq` UInt64, `SimTime` Double, `JobId` String, `Reason` String, `NewState` String.
  - `EVENT_TYPE_NAMES = {EventKind.STATE_CHANGED: "StateChangedEventType", EventKind.JOB_COMPLETED: "JobCompletedEventType", EventKind.JOB_FAILED: "JobFailedEventType"}`; basisnaam `"TerminalEventType"`.
  - `async def register_event_types(server: Server, idx: int) -> EventTypes` (`server.create_custom_event_type(idx, "TerminalEventType", ua.ObjectIds.BaseEventType, list(EVENT_FIELDS))`, subtypes met `properties=[]`).
  - `async def resolve_event_types(client: Client) -> EventTypes` (browse onder `client.nodes.base_event_type` op `f"{idx}:TerminalEventType"` en de drie subtypes; `idx` via `NAMESPACE_URI`).
  - `class EventEmitter` met `@classmethod async def create(cls, server: Server, types: EventTypes, node: Node, equipment_id: str) -> EventEmitter` (per kind `server.get_event_generator(types.<kind>)`, `gen.event.SourceNode = node.nodeid`, `gen.event.SourceName = equipment_id`), attribuut `seq: int` (start 0), `async def emit(self, ev: EquipmentEvent) -> None` (onder `asyncio.Lock`: `seq += 1`, velden uit `event_fields(ev)` + `Seq`, `Severity` = 500 bij `JOB_FAILED` of `new_state is State.FAULT` anders 100, `trigger(message=message_for(ev))`).
  - `def message_for(ev: EquipmentEvent) -> str` = `f"{ev.kind.value} {ev.equipment_id}"` + `f" job={ev.job_id}"` als job_id + `f" reason={ev.reason}"` als reason + `f" state={ev.new_state.value}"` als new_state.
  - `def event_from_ua(event: Any, types: EventTypes) -> tuple[int, EquipmentEvent]` → `(seq, EquipmentEvent)`; `ValueError` bij onbekend type/ontbrekend veld (via `types.kind_of(event.EventType)` en `event_from_fields(kind, {name: getattr(event, name) ...})`).
  - `EVENT_QUEUE = 100` en `async def subscribe_terminal_events(client: Client, sub: Any, types: EventTypes) -> int` = `await sub.subscribe_events(client.nodes.server, evtypes=[types.base], queuesize=EVENT_QUEUE)`.
- Produces in `nodeset.py`: `Types.events: EventTypes`; `register_types` roept `register_event_types(server, idx)` aan. `EQUIPMENT_VARS` zonder `LastEvent`/`EventSeq`.
- Produces in `tests/opcua/event_sink.py`:

```python
class EventSink:
    """Collects (seq, EquipmentEvent) from a client's Server node; unknown events go to .unknown."""
    def __init__(self): self.received: list[tuple[int, EquipmentEvent]] = []; self.unknown: list[object] = []
    async def attach(self, client) -> None: ...   # resolve_event_types + create_subscription(10, self) + subscribe_terminal_events
    async def event_notification(self, event) -> None: ...  # event_from_ua; ValueError -> self.unknown
    def events(self) -> list[EquipmentEvent]: ...
    async def wait_for(self, n: int, timeout: float) -> bool: ...  # poll elke 0.01 s tot len(received) >= n
    async def detach(self) -> None: ...
```

- [ ] **Step 1: Schrijf `tests/opcua/test_events.py`** (fixture: in-process `Server` op vrije poort + `Client`, zoals `tests/opcua/test_nodeset.py::server`):

```python
async def test_event_types_registered(server_client):
    server, client, idx, types = server_client
    r = await resolve_event_types(client)
    assert r == types
    base = client.get_node(types.base)
    names = {(await p.read_browse_name()).Name for p in await base.get_properties()}
    assert {"EquipmentId", "Seq", "SimTime", "JobId", "Reason", "NewState"} <= names
    for sub in (types.state_changed, types.job_completed, types.job_failed):
        parents = await client.get_node(sub).get_referenced_nodes(refs=ua.ObjectIds.HasSubtype, direction=ua.BrowseDirection.Inverse)
        assert parents[0].nodeid == types.base

async def test_three_kinds_received_with_seq(server_client, agv_obj):
    ...  # EventSink.attach(client); EventEmitter.create(...); emit STATE_CHANGED(BUSY), JOB_COMPLETED(J1), JOB_FAILED(J1, "X")
    assert await sink.wait_for(3, 2.0)
    assert [s for s, _ in sink.received] == [1, 2, 3]
    assert sink.events() == [ev1, ev2, ev3]

async def test_source_node_is_equipment_object(server_client, agv_obj):
    ...  # sink bewaart ook raw events: event.SourceNode == agv_obj.nodeid, event.SourceName == "AGV-01"

async def test_severity_500_for_failure_and_fault(server_client, agv_obj):
    ...  # JOB_FAILED -> 500, STATE_CHANGED FAULT -> 500, STATE_CHANGED IDLE -> 100

async def test_concurrent_emits_no_lost_seq(server_client, agv_obj):
    ...  # asyncio.gather van 20 emits; seqs == list(range(1, 21)) in enige volgorde, geen dubbele

async def test_kind_of_unknown_raises(server_client):
    with pytest.raises(ValueError):
        types.kind_of(ua.NodeId(ua.ObjectIds.SystemEventType))
```

- [ ] **Step 2: Pas `tests/opcua/test_nodeset.py` aan**: verwijder `test_emit_event_*`; `test_create_agv_object_and_read` assert dat `LastEvent`/`EventSeq` **niet** als kind bestaan; `test_types_registered` assert `types.events.base` bestaat.
- [ ] **Step 3: Run** `uv run pytest tests/opcua/test_events.py tests/opcua/test_nodeset.py -q` → FAIL.
- [ ] **Step 4: Implementeer `events.py`, `event_sink.py`, pas `nodeset.py` aan.**
- [ ] **Step 5: Run** → PASS.
- [ ] **Step 6: Commit** `feat(opcua): TerminalEventType hierarchy, EventEmitter and client decoder`

### Task 3: Sim-servers zenden events

**Files:**
- Modify: `packages/sim/src/terminal_sim/opcua_servers.py:55-90`
- Test: `tests/sim/test_opcua_servers.py`

**Interfaces:**
- Consumes: `EventEmitter.create(server, types.events, obj, equipment_id)`, `EventSink`.
- Produces: `EquipmentServer` gedrag ongewijzigd naar buiten; `sync()` flusht `_pending` via `emitter.emit`.

- [ ] **Step 1: Herschrijf de drie event-tests** met `EventSink` op `rig.client`:
  - `test_assign_via_opcua_runs_job`: na afloop `sink.events()` bevat een `JOB_COMPLETED` met `job_id == "J1"`.
  - `test_two_events_one_step_both_delivered`: twee events in één `sync()` → beide ontvangen, seqs opeenvolgend.
  - `test_events_delivered_in_order` (hernoemd van `..._via_last_event`): drie `on_emit` + één `sync()` → `[e.job_id for e in sink.events()] == ["J1", "J2", "J3"]`.
- [ ] **Step 2: Run** `uv run pytest tests/sim/test_opcua_servers.py -q` → FAIL.
- [ ] **Step 3: `EquipmentServer.start`: `self._emitter = await EventEmitter.create(server, types.events, self._obj, self.equipment.equipment_id)`** (na `create_equipment_object`, vóór `server.start()`).
- [ ] **Step 4: Run** → PASS. Ook `uv run pytest tests/sim -q` → PASS.
- [ ] **Step 5: Commit** `feat(sim): equipment servers emit OPC UA events`

### Task 4: Port abonneert zich op events

**Files:**
- Modify: `packages/opcua/src/terminal_opcua/port.py` (moduledocstring; `_Equipment` zonder `initial_seq`/`fifo`; `_Handler.event_notification`; `_connect`; verwijder `_on_event_seq`, `_handle_event_seq`, `_seq_lock`, `collections`-FIFO)
- Test: `tests/opcua/test_opcua_port.py`

**Interfaces:**
- Consumes: `resolve_event_types`, `subscribe_terminal_events`, `event_from_ua`.
- Produces: `_Equipment.last_seq: int`; `OpcUaEquipmentPort._on_event(event: Any) -> None` (sync: decodeer, gapcheck, `self._events.put`); `_Handler.event_notification(self, event)` roept het aan in try/except met `log.exception`.

- [ ] **Step 1: Pas tests aan**
  - `test_initial_values_not_delivered`: ongewijzigd in intentie (na connect geen events in `drain_events()`).
  - `test_assign_and_drain_events`: ongewijzigd.
  - `test_events_seq_gap_logged`: injecteer een gat door op de gateway-mirror de emitter-`seq` met 1 te verhogen (`mirror._emitter.seq += 1`) vóór een nieuw event; verwacht warning `"Seq gap"` in caplog en het event wél geleverd.
  - Verwijder `test_event_seq_handled_in_creation_order_with_direct_read` en `_append`.
  - Nieuw `test_two_events_one_sync_in_order`: `rig.server.on_emit(JOB_COMPLETED J9)`, `on_emit(STATE_CHANGED IDLE)`, één `sync()` → `drain` levert `[J9-event, IDLE-event]` in die volgorde.
  - Nieuw `test_unknown_event_type_ignored`: roep `port._on_event(SimpleNamespace(EventType=ua.NodeId(ua.ObjectIds.SystemEventType)))` → geen exception, `drain_events() == 0`, warning gelogd.
- [ ] **Step 2: Run** `uv run pytest tests/opcua/test_opcua_port.py -q` → FAIL.
- [ ] **Step 3: Implementeer**: in `_connect` na browse `self._types = await resolve_event_types(client)`; `sub = create_subscription(SUBSCRIPTION_PERIOD_MS, _Handler(self))`; `await subscribe_terminal_events(client, sub, self._types)`; `SimTime` datachange blijft. `_on_event`: `seq, ev = event_from_ua(event, self._types)` (ValueError → `log.warning`, return); `eq = self._equipment.get(ev.equipment_id)` (None → warning, return); `if seq > eq.last_seq + 1 and eq.last_seq: log.warning("%s: Seq gap %d -> %d", ...)`; `eq.last_seq = max(eq.last_seq, seq)`; `self._events.put(ev)`.
- [ ] **Step 4: Run** → PASS; `uv run pytest tests/opcua -q` → PASS.
- [ ] **Step 5: Commit** `feat(opcua): port consumes OPC UA events instead of LastEvent/EventSeq`

### Task 5: Gateway stuurt events door

**Files:**
- Modify: `packages/gateway/src/terminal_gateway/mirror.py` (`EquipmentMirror`: docstring, `__init__`, `start`, `_on_connected`, `event_notification`, `_announce_recovery`, `_on_lost`; verwijder `_is_event`, `_write_event`, `_deliver_upstream`, `_le_queue`, `_offset`, `_delivered_*`, `_last_upstream_le`, `_seq`)
- Modify: `packages/gateway/src/terminal_gateway/gateway.py` indien `_create_mirrors` `types` doorgeeft (geen wijziging verwacht).
- Test: `tests/gateway/test_gateway.py`

**Interfaces:**
- Consumes: `EventEmitter.create(self.server, self.types.events, self.obj, self.equipment_id)` in `start`; `resolve_event_types(client)` + `subscribe_terminal_events(client, sub, up_types)` in `_on_connected`; `event_from_ua`.
- Produces: `EquipmentMirror._emitter: EventEmitter`; `_up_seq: int` (laatst gezien upstream-seq, 0 na (her)verbinding); `async def event_notification(self, event) -> None`; `async def _emit(self, ev: EquipmentEvent) -> None` (= `await self._emitter.emit(ev)`).

- [ ] **Step 1: Herschrijf de event-tests** met `EventSink` op `rig.client` (vervang `EventConsumer`):
  - `test_mirror_reflects_variable_change`: na BUSY → `sink.events()[-1].new_state == State.BUSY`, seq 1.
  - `test_gateway_before_sim_recovers`: sink vóór `server.start()`; verwacht in volgorde: STATE_CHANGED FAULT (`UPSTREAM_DISCONNECTED`), STATE_CHANGED IDLE (`UPSTREAM_RECONNECTED`); daarna BUSY-event met seq 3.
  - `test_server_drop_marks_fault`: FAULT-event met `reason == "UPSTREAM_DISCONNECTED"`.
  - `test_three_events_in_one_sync_delivered_in_order`: job_ids `["J1","J2","J3"]`, seqs opeenvolgend.
  - `test_blip_with_one_event_delivers_it` → hernoem `test_blip_with_one_event_logs_gap`: event tijdens cut gaat verloren; na herstel een warning `"Seq gap"` (caplog) en recovery-event; een volgend event komt aan.
  - `test_blip_without_events_writes_no_last_event` → `test_blip_without_events_only_fault_and_recovery`: precies twee events (FAULT, recovery IDLE).
  - `test_blip_on_idle_upstream_reports_recovery_to_idle`, `test_blip_on_faulted_upstream_sends_no_recovery`: ongewijzigd in intentie.
  - Nieuw `test_blip_on_busy_upstream_reports_busy`: `rig.sts.set_state(State.BUSY)` + sync vóór cut; recovery-event `new_state == State.BUSY`.
  - `test_fresh_upstream_restart_emits_no_empty_event` → `test_fresh_upstream_restart_keeps_gateway_seq_monotonic`: na herstart van de upstream (seq weer 1) zijn gateway-seqs strikt oplopend en geen warning `"not ahead"`.
  - Nieuw `test_event_for_other_equipment_ignored`: `mirror.event_notification(SimpleNamespace(EventType=up_types.job_completed, EquipmentId="AGV-99", Seq=1, SimTime=0.0, JobId="J", Reason="", NewState=""))` → geen event op de gateway.
- [ ] **Step 2: Run** `uv run pytest tests/gateway -q` → FAIL.
- [ ] **Step 3: Implementeer** per spec §5: `_on_connected` schrijft alle snapshot-variabelen, zet `_up_seq = 0`, recovery-event als `_fault_announced` en upstream-state ≠ FAULT, abonneert datachange op snapshot-variabelen én events op `client.nodes.server`. `event_notification`: decodeer (ValueError → warning); `ev.equipment_id != self.equipment_id` → return; gap (`seq > _up_seq + 1 and _up_seq`) → `log.warning("%s: upstream Seq gap %d -> %d", ...)`; `_up_seq = seq`; `await self._emit(ev)`. `_on_lost`: `State = FAULT` + `_emit(STATE_CHANGED FAULT UPSTREAM_DISCONNECTED)` eenmalig.
- [ ] **Step 4: Run** `uv run pytest tests/gateway tests/opcua -q` → PASS.
- [ ] **Step 5: Commit** `feat(gateway): forward upstream OPC UA events with own Seq`

### Task 6: Volledige suite, e2e over OPC UA, documentatie

**Files:**
- Modify: `README.md` (sectie OPC UA-modus), `docs/superpowers/specs/2026-11-10-plan-b1-opcua-design.md` (§4 verwijzing naar B3-spec)
- Test: hele suite + `-m e2e_opcua`

- [ ] **Step 1: Run** `uv run pytest -q` → alle default tests PASS (incl. `tests/test_architecture.py`, ECS-hash).
- [ ] **Step 2: Run** `uv run pytest -m e2e_opcua -q` → 3 PASS; noteer in het taakrapport: ×25-equivalentie tellingen en moves/h exact gelijk.
- [ ] **Step 3: Run** `grep -rn "LastEvent\|EventSeq\|event_to_json\|event_from_json" packages tests README.md` → geen treffers buiten de B1-spec (historisch) en de B3-spec.
- [ ] **Step 4: README**: vervang de `LastEvent`/`EventSeq`-uitleg door de EventTypes-tabel (spec §2) en een asyncua-voorbeeld van 6 regels (`resolve_event_types` + `subscribe_terminal_events`). B1-spec §4: één regel "Events: zie Plan B3-spec (vervangt LastEvent/EventSeq)".
- [ ] **Step 5: Commit** `docs: OPC UA events in README; B1 spec points to B3`
