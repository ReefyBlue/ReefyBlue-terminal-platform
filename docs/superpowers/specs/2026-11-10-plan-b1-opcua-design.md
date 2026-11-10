# Terminal Platform — Plan B1: OPC UA-laag

Datum: 2026-11-10
Status: goedgekeurd in gesprek; aanvulling op `2026-11-10-terminal-platform-fase1-design.md` §2, §3, §7
Bouwt op: Plan A (`master` na PR #1)

## 1. Doel

De in-process kern wordt een platform van losse processen die via OPC UA praten. De ECS (`terminal_ecs`) wijzigt niet: hij krijgt een `OpcUaEquipmentPort` in plaats van `SimEquipmentPort`. Daarmee is bewezen dat gesimuleerde apparatuur later door vendor-adapters achter dezelfde gateway vervangen kan worden.

**Succescriteria**
- Baseline draait via sim-proces + gateway + runner (OPC UA) en levert dezelfde KPI's als de in-process run voor dezelfde seed (tolerantie: 0 voor tellingen; tijdwaarden binnen één steekproefinterval van 60 s sim-tijd; afwijkingen worden onderzocht en gerapporteerd)
- `terminal_ecs` is ongewijzigd (git diff leeg) en importeert alleen `terminal_model`
- Een weggevallen equipment-server verschijnt als `Fault` in de gateway en herstelt na reconnect

**Buiten scope B1**: beveiliging (anonymous, localhost), Docker, viewer, TOS-HTTP-API, eigen OPC UA DataTypes en Events (Plan B3).

## 2. Processen en klok

| Proces | CLI | Inhoud |
|---|---|---|
| `sim` | `python -m terminal_sim.serve scenario.yaml [--layout terminal.yaml] [--base-port 48400]` | Salabim-wereld + 12 equipment-servers + 1 klokserver, één klok |
| `gateway` | `python -m terminal_gateway.serve [--config terminal.yaml] [--port 4840]` | aggregeert alle servers onder één namespace |
| `runner` | `python -m terminal_runner.run --port opcua --gateway opc.tcp://localhost:4840 scenario.yaml` | ECS + TOS-stub + KPI's via `OpcUaEquipmentPort` |

**Pacing.** De sim loopt op wandklok × `speed_factor`: elke `TICK_WALL_S = 0.1` s wandklok draait `env.run(till = now + 0.1 × speed_factor)`. `speed_factor` uit het scenario krijgt hiermee zijn functie. De klokserver publiceert `Terminal/Clock/SimTime` (Double, s) en `SpeedFactor`.

**Sim-tijd in de ECS.** `OpcUaEquipmentPort.now()` leest `SimTime`. Dispatcher-watchdog en KPI-steekproeven blijven in sim-tijd. De runner-lus wacht op wandklok tot `SimTime` 60 s is opgelopen en roept dan `tick()` en `sample()` aan.

**Einde van een run.** De runner stopt op `status()["open"] == 0` of de duur-cap in sim-tijd, en stuurt daarna `Stop` naar de klokserver (methode) zodat het sim-proces netjes eindigt; `serve` accepteert ook SIGTERM.

## 3. Overdracht in de sim

De hooks `on_transfer` verdwijnen uit de runner (ook in-process); de sim handelt boxoverdracht zelf af, op basis van wat hij al weet:

| Moment | Actie in sim |
|---|---|
| STS `DISCHARGE` voltooid | box op de AGV in de baan die voor deze `container_id` gestuurd is (`job_container_id`); geen passende AGV → `fail_job("NO_AGV_IN_LANE")` |
| AGV `TRANSPORT` naar `TP/<blok>/WS` voltooid met box | box naar TP; `tp_ws_occupied = True` op de ASC |
| ASC `STACK_IN` voltooid | `tp_ws_occupied = False` |
| ASC `STACK_OUT` voltooid | box ligt op TP; `tp_ws_occupied = True` |
| AGV `TRANSPORT` naar `TP/<blok>/WS` voltooid zonder box, TP bezet door box met zijn `job_container_id` | AGV laadt; `tp_ws_occupied = False` |
| STS `LOAD` voltooid | AGV in baan wordt leeg |

`World.transfer_*` blijft bestaan voor tests maar wordt niet meer door de runner aangeroepen. Twee boxen op één TP: de tweede wacht op de AGV (AGV blijft BUSY op het TP tot het vrij is) — consistent met het ECS-model van Plan A (TP-capaciteit 1 voor losboxen).

## 4. OPC UA-informatiemodel (pragmatisch)

Namespace-URI `urn:terminal:model`, index toegekend bij registratie.

**`EquipmentType`** (abstract ObjectType)
- Variabelen: `Id` (String), `Kind` (String), `Vendor` (String), `State` (String: Idle/Busy/Fault/Maintenance), `PositionX`, `PositionY` (Double, m), `CurrentJobId` (String, leeg = geen), `EnergyKwh` (Double), `Extra` (String, JSON van `snapshot().extra`), `LastEvent` (String, JSON van `EquipmentEvent`), `EventSeq` (UInt64, start 0, +1 per event)
- Methoden: `AssignJob(jobJson: String) → Boolean`, `CancelJob() → Boolean`, `Reset() → Boolean`

**Subtypen** voegen alleen de eigen `extra`-velden toe als losse variabelen (zie spec §3): `STSType` (BerthSide, SpreaderHeightM, TrolleyPositionM, HandoverLaneOccupied), `AGVType` (BatteryPct, LoadedContainerId, Route als JSON-string, SpeedMps), `ASCType` (BlockId, GantryPositionM, WsTpOccupied, LsTpOccupied). `Extra` blijft daarnaast bestaan zodat `snapshot().extra` verliesvrij overkomt.

**`ControlType`** (object `Terminal/Clock`): `SimTime` (Double), `SpeedFactor` (Double), methoden `Stop()` en `PrefillYard(json: String) → Boolean` (lijst van `{"slot": "A/1/1/1", "container_id": "C0101"}`; alleen toegestaan vóór de eerste job).

**JSON-codecs** (`terminal_opcua.codec`): `job_to_json(Job) / job_from_json(str)`, `event_to_json(EquipmentEvent) / event_from_json(str)`; enums als hun stringwaarde, `None` als JSON `null`, `Position` als `{"x":..,"y":..}`. Round-trip-tests verplicht.

**Namespace-boom in de gateway** exact als spec §3: `Terminal/Clock` (ControlType), `Terminal/Quay/STS-xx`, `Terminal/Fleet/AGV-xx`, `Terminal/Yard/Block-X/ASC-X`. `Terminal/Jobs/` blijft leeg in B1 (de ECS houdt zijn eigen job-administratie).

## 5. Equipment-servers (in het sim-proces)

- Eén asyncua-server per apparaat: poort `base_port + index` in de volgorde van `World.equipment` (STS, AGV, ASC), controlserver (ControlType) op `base_port + 12`. Endpoint `opc.tcp://0.0.0.0:<poort>/terminal/<equipment_id>`.
- Salabim en asyncio delen één thread: een asyncio-taak doet de pacing (§2) en roept `env.run(till=…)` aan; methode-aanroepen van clients komen via asyncua op dezelfde event loop binnen en worden direct op het Equipment uitgevoerd (de simloop staat op dat moment stil tussen twee ticks). Geen threads, geen locks.
- `emit` van elk Equipment schrijft `LastEvent` en verhoogt `EventSeq`; daarna worden `State`, `CurrentJobId`, positie en `Extra` bijgewerkt. Variabelen worden na elke tick gesynchroniseerd vanuit `snapshot()` (niet per Salabim-stap).
- `FaultInjector` blijft werken zoals in Plan A.

## 6. Gateway

- asyncua-server op poort 4840, endpoint `opc.tcp://0.0.0.0:4840/terminal/gateway`.
- Leest `terminal.yaml` (uitgebreid met per apparaat een `endpoint`-veld, of afgeleid uit `base_port`); per apparaat één asyncua-client met data-change-subscriptions op alle variabelen (publishing interval 50 ms); spiegelt naar de eigen namespace.
- Methode-aanroepen op gateway-objecten worden doorgestuurd naar de onderliggende server; de returnwaarde wordt teruggegeven; onbereikbaar → `False` en een logregel.
- Verbinding weg → `State = Fault` in de gateway, `LastEvent` met `STATE_CHANGED Fault`, reconnect elke 5 s; na herstel volgt een volledige hersynchronisatie.
- Geen logica, geen caching van jobs.

## 7. `OpcUaEquipmentPort` (`terminal_opcua.port`)

- Constructor `OpcUaEquipmentPort(endpoint: str)`; `connect()`/`close()` (context manager). Intern een asyncio-lus in een eigen thread; alle publieke methoden zijn synchroon en blokkeren tot het antwoord er is.
- `equipment_ids()`: browse van `Terminal/Quay|Fleet|Yard`, gesorteerd zoals in-process (STS, AGV, ASC in id-volgorde).
- `snapshot(id)`: leest de variabelen in één `read_values`-aanroep; `extra` uit `Extra`-JSON.
- `assign_job/cancel_job/reset`: methode-aanroep; `KeyError` voor onbekende id.
- `subscribe(cb)`: registreert `cb`. Een data-change op `EventSeq` zet het event (uit `LastEvent`) op een thread-safe queue. **Aflevering**: `drain_events()` levert de wachtende events in volgorde af aan alle callbacks; de runner roept `drain_events()` aan in zijn lus (elke 0,1 s wandklok) en vóór elke `tick()`. Zo blijft de dispatcher single-threaded.
- `now()`: leest `SimTime`.
- Gemiste `EventSeq`-nummers worden gelogd als WARNING (geen herhaalmechanisme in B1).

## 8. Runner

- `--port inprocess|opcua` (default inprocess), `--gateway URL`. In opcua-modus: geen World; `port = OpcUaEquipmentPort(url)`. De TOS-stub genereert de werkorders zoals altijd; `prefill` gaat in opcua-modus via `port.prefill_yard(slots)` → methode `PrefillYard` op de gateway (doorgestuurd naar de sim). Zo blijft `terminal_sim` onafhankelijk van `terminal_tos_stub`. `OpcUaEquipmentPort.prefill_yard` is een extra methode buiten het `EquipmentPort`-protocol; de runner roept hem alleen aan in opcua-modus.
- KPI's identiek; CSV/JSON-uitvoer identiek.

## 9. Pakketten en afhankelijkheden

| Package | Hangt aan | Nieuw |
|---|---|---|
| `terminal_opcua` | model, asyncua | namespace-definitie (`nodeset.py`), `codec.py`, `port.py` |
| `terminal_sim` | model, opcua, salabim, pyyaml | `serve.py`, `opcua_servers.py`, autonome overdracht in equipment/world |
| `terminal_gateway` | model, opcua, pyyaml | `serve.py`, `gateway.py` |
| `terminal_runner` | alles | `--port opcua` |
| `terminal_ecs` | model | **ongewijzigd** |

Architectuurtest uitgebreid: `opcua` importeert alleen model; `gateway` alleen model + opcua; `ecs` ongewijzigd.

## 10. Foutafhandeling

| Situatie | Gedrag |
|---|---|
| Gateway niet bereikbaar bij start runner | runner retry 10× elke 2 s, dan exit 1 met melding |
| Equipment-server valt weg | gateway `Fault` + event; ECS sluit uit en herplant (bestaand gedrag) |
| Methode-aanroep time-out (>2 s) | `False` naar ECS, WARNING-log |
| `EventSeq`-gat | WARNING; snapshot blijft leidend voor state |
| `AssignJob` met ongeldige JSON | server geeft `False` en logt |
| Sim-proces loopt achter op pacing (tick duurt > 0,1 s wandklok) | sim haalt in zonder te wachten; WARNING als achterstand > 5 s wandklok |

## 11. Testen

- Unit: codec round-trips; namespace-opbouw (typen, variabelen, methoden aanwezig); `OpcUaEquipmentPort` voldoet aan `isinstance(port, EquipmentPort)`.
- Integratie (echte asyncua-servers op vrije poorten, in één pytest-proces): STS-server + client: `AssignJob` → `EventSeq` +1 en `LastEvent` JOB_COMPLETED; gateway spiegelt een variabele binnen 200 ms en stuurt `Reset` door; wegvallen van een server → gateway `Fault`, terugkomen → `Idle`.
- Contract: de bestaande ECS-tests met `FakePort` draaien ongewijzigd (ECS onveranderd).
- Autonome overdracht: in-process baseline zonder hooks levert exact dezelfde KPI's als Plan A met hooks (vóór en na de wijziging vergelijken; vastleggen in test).
- End-to-end: sim-proces + gateway + runner (`speed_factor` 200, 20 containers, 1 STS, 2 AGV's) voltooit 20/20 in < 60 s wandklok; baseline 200 containers via OPC UA vs in-process: tellingen gelijk, tijd-KPI's binnen 60 s sim-tijd. Deze test is gemarkeerd `e2e_opcua` en draait in de standaard suite als de totale suite onder ~3 minuten blijft, anders alleen op aanvraag (`-m e2e_opcua`).

## 12. Pad naar B2 en B3

- **B2**: ECS als FastAPI-service (werkorders-API, status, websocket-events), TOS-stub als HTTP-client, webviewer (Canvas 2D) op de ECS-eventstroom, Docker Compose voor de NAS, duurtest 48–72 uur realtime met storingen (geheugen, logs, reconnects, KPI-stabiliteit).
- **B3**: eigen OPC UA DataTypes voor Job en Event, OPC UA Events met EventTypes, beveiliging (certificaten, gebruikers), eerste vendor-adapter achter de gateway.
