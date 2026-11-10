# Plan B3 — Echte OPC UA Events (ontwerp)

Datum: 2026-11-10. Bouwt op Plan B1 (`2026-11-10-plan-b1-opcua-design.md`).

## 1. Doel en scope

B1 vervoert equipment-events als JSON in de variabele `LastEvent`, gekoppeld aan de teller `EventSeq`. Dat werkt, maar is geen OPC UA en vereist fragiele FIFO-koppeling in gateway en port. B3-stap 1 vervangt dit door **echte OPC UA Events** met eigen EventTypes.

**In scope**
- Eigen EventTypes in namespace `urn:terminal:model`.
- Sim-servers zenden events; gateway stuurt ze door; port abonneert zich.
- `LastEvent`/`EventSeq` en alle bijbehorende koppel-/offsetlogica verdwijnen.

**Buiten scope** (latere B3-stappen): eigen DataTypes voor Job/Container, beveiliging (certificaten, gebruikers), event-history, vendor-adapter. `EquipmentPort`-protocol, ECS, TOS-stub, runner-lus en KPI's blijven ongewijzigd.

## 2. EventTypes

| Type | Supertype | Eigen velden |
|---|---|---|
| `TerminalEventType` | `BaseEventType` | `EquipmentId` String, `Seq` UInt64, `SimTime` Double, `JobId` String, `Reason` String, `NewState` String |
| `StateChangedEventType` | `TerminalEventType` | — |
| `JobCompletedEventType` | `TerminalEventType` | — |
| `JobFailedEventType` | `TerminalEventType` | — |

Afbeelding op `EquipmentEvent`: `kind` ← subtype; `equipment_id` ← `EquipmentId`; `sim_time` ← `SimTime`; `job_id`/`reason`/`new_state` ← bijbehorend veld, lege string = `None`.

Standaardvelden bij uitzenden: `SourceNode` = het equipment-object, `SourceName` = equipment-id, `Message` = `"<KIND> <equipment-id>"` plus job-id/reason als aanwezig, `Severity` = 500 bij `JobFailedEventType` en bij `StateChangedEventType` naar `FAULT`, anders 100, `Time` = wandklok.

`Seq` is een teller per apparaat, begint bij 1 na start van de uitzendende server en is strikt oplopend. Hij dient uitsluitend voor gapdetectie bij ontvangers.

## 3. Namespace-code (`terminal_opcua`)

- Nieuwe module `terminal_opcua/events.py` met `EventTypes` (NodeIds `base`, `state_changed`, `job_completed`, `job_failed`), `register_event_types(server, idx) -> EventTypes` (server-kant, `create_custom_event_type`) en `resolve_event_types(client) -> EventTypes` (client-kant, via browse-namen onder `BaseEventType`, zodat NodeIds niet hoeven overeen te komen). `nodeset.register_types` roept `register_event_types` aan en neemt het resultaat op als `Types.events`.
- `EQUIPMENT_VARS`/`VAR_TYPES` verliezen `LastEvent` en `EventSeq`.
- Emitting node is het `Server`-object (asyncua levert events alleen aan abonnees op de emitting node; de `HasNotifier`-keten wordt niet gevolgd). `SourceNode` = het equipment-object, `SourceName` = equipment-id.
- `EventEmitter.create(server, types: EventTypes, node, equipment_id)` houdt één `EventGenerator` per EventType en de `Seq`-teller achter een `asyncio.Lock`. `emit(ev)` zet de velden en roept `trigger()` aan.
- `codec`: `event_to_json`/`event_from_json` verdwijnen; nieuw `event_fields(ev) -> dict[str, object]` (pure) en `event_from_fields(kind, fields) -> EquipmentEvent` (pure; `ValueError` bij ontbrekend veld of onbekende state). `events.event_from_ua(event, types) -> tuple[int, EquipmentEvent]` (`Seq` plus event) beeldt `event.EventType` af op `EventKind` (`ValueError` bij onbekend type) en roept `event_from_fields` aan. Job-codec blijft.
- Abonneren (gateway en port): `sub.subscribe_events(client.nodes.server, evtypes=[types.base], queuesize=100)`; de where-clause die asyncua genereert dekt de subtypes.

## 4. Sim-servers (`terminal_sim.opcua_servers`)

Structuur ongewijzigd: `EquipmentServer.on_emit(ev)` plant `EventEmitter.emit(ev)` op de serverloop. Eén apparaat per server. `ControlServer` ongewijzigd.

## 5. Gateway (`terminal_gateway.mirror`)

- `EquipmentMirror` abonneert zich bij `_on_connected` op events van het upstream `Server`-object (sectie 3) naast de datachange-abonnementen op de snapshot-variabelen.
- `event_notification(event)`: decodeer naar `EquipmentEvent`; log een warning bij een gat in upstream-`Seq`; zend opnieuw uit via de eigen `EventEmitter` op het spiegelobject (eigen gateway-`Seq`). Negeer events waarvan `EquipmentId` niet dit apparaat is.
- Verbindingsverlies (`_on_lost`): `State = FAULT` + `StateChangedEvent(Reason = UPSTREAM_DISCONNECTED, NewState = FAULT)`, één keer per outage.
- Herverbinding (`_on_connected`): snapshot-variabelen overnemen; als er een FAULT gesynthetiseerd was en upstream niet zelf FAULT is: `StateChangedEvent(Reason = UPSTREAM_RECONNECTED, NewState = <upstream State>)`.
- Weg: `_le_queue`, `_offset`, `_delivered_*`, `_last_upstream_le`, `_write_event`, `_deliver_upstream`, `_is_event`. Events die tijdens een outage upstream zijn uitgezonden gaan verloren; dat is zichtbaar als `Seq`-gat in de log. De ECS behandelt het apparaat tijdens de outage al als FAULT en herplant via de snapshot (B1-gedrag).

## 6. Port (`terminal_opcua.port`)

- Eén event-abonnement op het gateway-`Server`-object (sectie 3). `event_notification` → `event_from_fields` → bestaande wachtrij die `drain_events()` leegt op de ECS-thread.
- Per apparaat `last_seq`; gat → warning, geen verdere actie.
- Weg: `_on_event_seq`, `_handle_event_seq`, FIFO-koppeling, `LastEvent`-fallback-read. Datachange-abonnementen voor snapshot-variabelen blijven.
- `OpcUaEquipmentPort`'s publieke API en `EquipmentPort` blijven identiek.

## 7. Foutafhandeling

| Situatie | Gedrag |
|---|---|
| Event met onbekend EventType of ontbrekend veld | warning, event overgeslagen |
| `Seq`-gat (upstream→gateway of gateway→port) | warning met oude/nieuwe `Seq` |
| Event van ander `EquipmentId` op een mirror-link | genegeerd |
| Upstream zelf FAULT bij herverbinding | geen recovery-event; apparaat blijft uitgesloten |

## 8. Tests

- `tests/opcua`: EventTypes aanwezig met juiste velden en supertypes; in-process server→client: drie eventsoorten komen aan met juiste velden, `Seq` 1,2,3; `event_fields`/`event_from_fields` round-trip; onbekend type/veld → overgeslagen.
- `tests/gateway`: event doorgestuurd met eigen `Seq`; upstream-gat gelogd; outage via tcp-proxy → FAULT-event, herverbinding → recovery-event met upstream-state; upstream FAULT → geen recovery; event van ander apparaat genegeerd.
- `tests/opcua/port`: events landen in `drain_events()` in volgorde; gapdetectie-warning.
- `tests/e2e` (`e2e_opcua`): bestaande drie tests ongewijzigd in intentie; ×25-equivalentie: tellingen en moves/h exact gelijk aan in-process.
- Architectuurtest en ECS-hash-fixture blijven groen.

## 9. Documentatie

README-sectie "OPC UA-modus": `LastEvent`/`EventSeq` vervangen door EventTypes-tabel en een voorbeeld-abonnement. B1-spec §4 (informatiemodel) krijgt een verwijzing naar dit document.

## 10. Pad verder

Volgende B3-stappen, elk met eigen spec: DataTypes voor Job/Container (Structures i.p.v. JSON in `AssignJob`/`Extra`), beveiliging (certificaten, gebruikers, bind-adres), event-history als de `Seq`-gaten in de duurtest daarom vragen, eerste vendor-adapter achter de gateway.
