# Plan B2a — ECS als service (ontwerp)

Datum: 2026-11-10. Bouwt op Plan B1 (OPC UA-laag) en B3-stap 1 (echte OPC UA Events). Eerste van drie B2-deelprojecten: B2a ECS-service, B2b webviewer, B2c NAS-inzet + duurtest.

## 1. Doel en scope

De ECS draait nu alleen in de runner-lus. B2a maakt er een zelfstandig, langlopend proces van met een HTTP/WebSocket-API, zodat TOS-stub, runner en straks de viewer (B2b) er als clients mee praten en het geheel op de NAS (B2c) kan draaien.

**In scope**
- Pakket `terminal_ecs_service`: FastAPI-app + achtergrondlus rond `Dispatcher` + KPI-collector, verbonden met de gateway via `OpcUaEquipmentPort`.
- Pakket `terminal_kpi`: `KpiCollector` verhuist uit `runner`, krijgt een niet-destructieve tussenstand.
- TOS-stub als HTTP-client (`submit`-CLI), runner met `--port http`.
- Alleen observeren: werkorders indienen, status/KPI's/equipment lezen, eventstroom. Geen sim-besturing of operator-ingrepen via de ECS-API.

**Buiten scope**: viewer (B2b), Docker Compose/NAS/duurtest (B2c), authenticatie/TLS, persistentie, OPC UA DataTypes/beveiliging (B3). `terminal_ecs` (dispatcher, orders, workorders) blijft ongewijzigd; de hash-fixture blijft gelden.

## 2. Processen

| Proces | CLI | Rol |
|---|---|---|
| `sim` | ongewijzigd (`terminal_sim.serve`) | Salabim-wereld + OPC UA-servers |
| `gateway` | ongewijzigd (`terminal_gateway.serve`) | aggregatie |
| `ecs` | `python -m terminal_ecs_service.serve --gateway opc.tcp://localhost:4840 --layout terminal.yaml --host 0.0.0.0 --port 8000` | Dispatcher + KPI + API |
| `tos-stub` | `python -m terminal_tos_stub.submit scenario.yaml --layout terminal.yaml --ecs http://localhost:8000` | genereert orders (bestaande generator, seed uit scenario) en POST ze |
| `runner` | `python -m terminal_runner.run scenario.yaml --port http --ecs http://localhost:8000 --gateway opc.tcp://localhost:4840 --out out/` | orkestreert één run |

De runner in `--port http`: wacht op `GET /health` 200 (max. 30 s, anders exit 1), controleert de vloot via `GET /equipment` (zoals `_check_fleet`), doet `PrefillYard` via zijn eigen `OpcUaEquipmentPort` op de gateway, POST de orders, roept `Start` aan, pollt `GET /status` elke 0,5 s wandklok tot `open == 0` of de duur-cap in sim-tijd, haalt `GET /kpis`, roept `Stop` aan en schrijft CSV/summary zoals nu. `--port inprocess` en `--port opcua` blijven ongewijzigd werken.

## 3. API

FastAPI, Pydantic-schema's in `terminal_ecs_service.schemas`. Alle tijden in sim-seconden.

| Endpoint | Antwoord |
|---|---|
| `POST /workorders` body `[{"container_id","type":"DISCHARGE"\|"LOAD","sts_id","yard_slot"}]` | 202 `{"accepted": n}`; 422 `{"errors":[{"container_id","code","message"}]}` — batch wordt in zijn geheel afgewezen als één order faalt (`WorkOrderError`-codes `DUPLICATE_CONTAINER`, `UNKNOWN_STS`, `INVALID_SLOT`, en `INVALID_ORDER` voor een misvormde body); 503 als niet verbonden |
| `GET /status` | `Dispatcher.status()` + `sim_time`, `connected`, `uptime_s`, `tick_count` |
| `GET /kpis` | `KpiCollector.snapshot(elapsed)`: zelfde sleutels als `finish` (incl. `sim_elapsed_s`), niet-destructief |
| `GET /equipment` | `[{"equipment_id","kind","vendor","state","position":{"x","y"},"current_job_id","energy_kwh","extra"}]` |
| `GET /health` | 200 `{"connected": true, "sim_time": t}`; 503 `{"connected": false, "sim_time": null}`, of 503 `{"connected": true, "sim_time": t, "error": "sim_time_reversed"}` na een sim-herstart (§7) |
| `WS /events` | JSON-berichten, §5 |

Validatie: `type` moet `DISCHARGE` of `LOAD` zijn, `yard_slot` moet parsen als slot-locatie (`parse_location`), anders 422 vóór de Dispatcher. Elke geaccepteerde order registreert zijn `yard_slot` in de ECS-eigen allocator (`occupy`), zodat herslotten bij ASC-storing geen slot kiest dat de TOS al heeft uitgegeven.

## 4. Interne lus en concurrency

- Eén asyncio-loop (uvicorn). `EcsCore` bezit: `OpcUaEquipmentPort`, `Dispatcher(port, allocator)`, `SimpleSlotAllocator` (uit de layout), `KpiCollector`, de WS-clientenset.
- Achtergrondtaak `EcsCore.run()`: elke `DRAIN_POLL_S = 0.005` s wandklok `port.drain_events()`; zodra `port.now() >= next_tick` (grenzen van `SAMPLE_INTERVAL_S = 60` s sim-tijd vanaf `t0 = port.now()` bij verbinding): `dispatcher.tick()`, `kpi.sample()`, `next_tick += 60`, daarna `status`- en `job`-berichten naar WS-clients. Tussen ticks worden `drain_events()`-events ook aan de dispatcher geleverd (zoals de runner nu), zodat de ECS uit zijn eventcallback dispatcht.
- `EcsCore.step()` doet één `drain_events()` plus tick/sample als de grens bereikt is; `run()` roept `step()` in de lus aan. Tests en de in-process-equivalentie roepen `step()` direct aan en bewegen de World zelf.
- Alle endpoints zijn `async def` en raken dispatcher/KPI op de loop-thread → geen locks. `POST /workorders` roept `dispatcher.submit(orders)` en daarna `dispatcher.tick()` aan (eerste toewijzingen direct, zoals de runner na `submit`).
- Verbinding: bij start verbinden met `CONNECT_RETRY_S = 2` s tussen pogingen, onbeperkt; `connected=false` zolang dat niet lukt (health 503). `ConnectionError` uit de port tijdens de lus → `connected=false`, `link`-bericht, port sluiten, elke 2 s opnieuw verbinden met een nieuwe `OpcUaEquipmentPort`; de bestaande `Dispatcher` en `KpiCollector` krijgen de nieuwe port via `rebind(port)` (beide abonneren zich opnieuw; de Dispatcher houdt zijn orders en interne staat). Apparaten komen via de gateway-FAULT/recovery-events terug in dienst, zoals in B1/B3.
- `rebind(port)` op `Dispatcher` zou de ECS wijzigen (hash). Daarom: `EcsCore` geeft de Dispatcher een `ReconnectingPort`-wrapper die zelf het `EquipmentPort`-protocol implementeert, de huidige `OpcUaEquipmentPort` delegeert en abonnees bewaart; bij herverbinding wisselt de wrapper intern van port en abonneert zich opnieuw. Dispatcher en KPI zien één stabiele port. `now()` geeft tijdens een onderbreking de laatst bekende sim-tijd.

## 5. Eventstroom (`WS /events`)

Eén JSON-object per bericht, veld `type`:

| `type` | Velden | Wanneer |
|---|---|---|
| `status` | `sim_time` + alle sleutels van `Dispatcher.status()` | direct na verbinding van een client, en na elke tick |
| `equipment` | `kind`, `equipment_id`, `sim_time`, `job_id`, `reason`, `new_state` (null waar n.v.t.) | elk `EquipmentEvent` uit `drain_events()` |
| `job` | `job_id`, `equipment_id`, `sim_time` (uit `LoggedJob`) | elke nieuwe `job_log`-sleutel, gedetecteerd na elke tick en na elke `POST /workorders` |
| `link` | `connected`, `sim_time` | bij verlies en herstel van de gatewayverbinding |

Geen replay. Per client een wachtrij van 1000 berichten; vol → bericht gedropt en één warning per client per minuut. Verbroken client → opgeruimd.

## 6. Pakketten en afhankelijkheden

| Pakket | Mag afhangen van | Inhoud |
|---|---|---|
| `terminal_kpi` (nieuw, `packages/kpi`) | model, ecs | `kpi.py` verplaatst uit runner; `snapshot(duration_s) -> dict[str, float]` (zelfde sleutels als `finish`; `finish` roept `snapshot` aan en blijft bestaan) |
| `terminal_tos_stub` | model, ecs | `allocator.occupy(slot)` (idempotent; `KeyError` bij onbekend blok); nieuw `client.py` (`EcsClient(base_url)`: `post_workorders`, `status`, `kpis`, `equipment`, `wait_healthy(timeout_s)` — httpx) en `submit.py` (CLI) |
| `terminal_ecs_service` (nieuw, `packages/ecs_service`) | model, ecs, opcua, kpi, tos_stub | `core.py` (`EcsCore`), `port.py` (`ReconnectingPort`), `app.py` (`create_app(core) -> FastAPI`), `schemas.py`, `events.py` (WS-broadcaster), `serve.py` (CLI) |
| `terminal_runner` | alles | `run_once_http`; importeert `KpiCollector` uit `terminal_kpi` |

Nieuwe dependencies: `fastapi`, `uvicorn[standard]`, `httpx`, `websockets` (test-client); `terminal_ecs_service` vermeldt daarnaast `pyyaml` (layout laden) en `anyio` (WS-taakgroep) expliciet; dev-groep: `httpx2` (vereist door de Starlette `TestClient`). Architectuurtest (`tests/test_architecture.py`) krijgt de twee nieuwe pakketten met bovenstaande regels. ECS-hash-fixture ongewijzigd.

## 7. Foutafhandeling

| Situatie | Gedrag |
|---|---|
| Order ongeldig | 422 met alle fouten van de batch; niets aangenomen. De service valideert de hele batch zelf vóór `submit` (type, slot-syntaxis, bekende STS, dubbele container t.o.v. eerdere batches en binnen de batch), zodat `Dispatcher.submit` in de praktijk niet meer faalt; gooit `submit` toch een `WorkOrderError`, dan 500 met de fout en een logregel (orders vóór de fout in die batch zijn dan wel aangenomen) |
| Gateway onbereikbaar bij start | service start, health 503, blijft verbinden |
| Verbinding valt weg tijdens run | `link`-bericht, health 503, herverbinden; ECS-staat blijft. Events uit de onderbreking zijn weg: bij herstel levert `ReconnectingPort.bind` per apparaat waarvan de toestand afwijkt van de laatst bekende een `STATE_CHANGED` met `reason="LINK_RESTORED"` (na het verversen van de cache), zodat de Dispatcher bv. een FAULT→IDLE-apparaat weer vrijgeeft. Verloren `JOB_COMPLETED`/`JOB_FAILED` zijn zonder ECS-wijziging niet te reconstrueren: alleen een WARNING met de apparaten waarvan `current_job_id` veranderde; reconciliatie volgt in B2d |
| Sim herstart onder een draaiende ECS (sim-tijd gaat terug) | ERROR-log, `time_reversed`, `/health` 503 `{"connected": true, "sim_time": t, "error": "sim_time_reversed"}`; de lus draait door. Eén ECS-proces per sim-run tot B2c herstartgedrag vastlegt |
| Runner: ECS niet vers (`done + open + failed > 0` in `/status` na health) | exit 1 "ECS service is not fresh (…): restart it per run" |
| WS-client traag | berichten gedropt voor die client (zie §5) |
| `GET /kpis` vóór eerste tick | geldige tussenstand met nullen, `sim_elapsed_s = 0` |
| Runner: health niet binnen 30 s 200 | exit 1 met melding |
| Runner: vloot klopt niet | exit 1 (bestaande melding) |

## 8. Tests

- `tests/kpi`: bestaande KPI-tests verhuizen; `snapshot` verandert de collector niet (twee aanroepen gelijk; `finish` daarna gelijk aan `snapshot`).
- `tests/ecs_service` met de bestaande `fake_port`-fixture: lus doet tick/sample op 60 s-grenzen en drain tussendoor; `ReconnectingPort` wisselt na `ConnectionError` naar een nieuwe port en abonnees ontvangen weer events; WS-broadcaster dropt bij volle wachtrij; API via `httpx.AsyncClient(transport=ASGITransport(app))`: alle endpoints, 422-paden (type, slot, dubbele container, onbekende STS), 503 zonder verbinding, WS-handshake levert eerst `status`.
- Equivalentie: een `EcsCore` op `World.port` (in-process, geen OPC UA) met `create_app` achter een `TestClient`; de test POST de baseline-orders (seed 1), beweegt de World per 60 s en roept `core.step()` aan tot `open == 0`, en vergelijkt `GET /kpis` met `tests/e2e/fixtures/plan_a_baseline_seed1.json`. Dit pint vast dat de service-lus de runner-lus nabootst. `run_once_http` zelf wordt getest tegen een gescripte ECS (`httpx.MockTransport`) en in de e2e-test.
- `e2e_opcua`: sim + gateway + ecs-service (subprocess) + runner `--port http`, 20 containers, 1 STS, 2 AGV, voltooit; `GET /status` toont `open == 0`.
- Architectuurtest groen met de nieuwe regels; ECS-hash groen.

## 9. Documentatie

README: sectie "ECS als service" met de drie CLI's, API-tabel en WS-berichtsoorten; `--port http` bij de runner.

## 10. Pad verder

B2b viewer op `WS /events` + `GET /equipment`; B2c Docker Compose (sim, gateway, ecs, tos-stub, viewer), health checks, logging naar bestand, 48–72 u duurtest met `faults.yaml`. Parkeerpunten uit B1 die hier relevant worden: communicatieverlies telt als equipment-FAULT voor de ECS (gebruikersbeslissing nodig vóór de duurtest); meerdere replicaties over OPC UA vereisen een sim-herstart per run.
