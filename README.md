# Terminal Platform — fase 1 kern

Fase 1 van het terminalplatform voor een volautomatische containerterminal: een discrete-event simulator (salabim) van een fictieve terminal, de ECS-dispatchlogica erachter via de `EquipmentPort`, een TOS-stub en een scenario-runner die KPI's oplevert. De runner draait in-process of, sinds Plan B1, via OPC UA tegen een apart sim-proces achter een gateway (zie [OPC UA-modus](#opc-ua-modus-plan-b1)).

## Installeren

```
uv sync
```

## Draaien

```
uv run python -m terminal_runner.run scenarios/baseline.yaml --replications 10 --out out/
```

| Optie | Betekenis | Default |
|---|---|---|
| `--replications N` | aantal replicaties, seed = `scenario.seed + i` | 1 |
| `--out DIR` | uitvoermap voor `<name>.csv` (één rij per run) en `<name>-summary.json` (gemiddelde, p5, p95 per KPI) | `out` |
| `--layout FILE` | terminallayout | `terminal.yaml` |
| `--port inprocess\|opcua` | sim in-process (`World`) of via OPC UA (gateway) | `inprocess` |
| `--gateway URL` | gateway-endpoint bij `--port opcua` | `opc.tcp://localhost:4840` |

Tests: `uv run pytest -q` (inclusief de e2e-runs met 200 containers, marker `e2e`; uitsluiten met `-m "not e2e"`). De OPC UA-e2e-tests (marker `e2e_opcua`) staan standaard uit, zie [OPC UA-modus](#opc-ua-modus-plan-b1).

## Scenario-yaml (`scenarios/*.yaml`)

| Veld | Betekenis |
|---|---|
| `name` | naam; bepaalt de bestandsnamen van de uitvoer |
| `seed` | startseed (replicatie i gebruikt `seed + i`) |
| `n_sts` | aantal STS-kranen (1..2) |
| `n_agv` | aantal AGV's (1..6) |
| `n_containers` | aantal containers (werkorders) |
| `share_40ft` | aandeel 40ft-containers (0..1); niet gebruikt in Plan A (voorbereid) |
| `discharge_share` | aandeel lossen (1.0 = alleen lossen, 0 = alleen laden) |
| `speed_factor` | alleen in OPC UA-modus: sim-seconden per wandklokseconde van `terminal_sim.serve` (in-process draait de runner zo snel mogelijk in sim-tijd) |
| `duration_s` | harde bovengrens op de gesimuleerde tijd (s) |
| `faults` | optioneel; per `sts`/`agv`/`asc`: `mtbf_s`, `mttr_s` (mtbf 0 = geen storingen) |

## KPI-kolommen

| Kolom | Betekenis |
|---|---|
| `moves_per_hour_total` | moves per gesimuleerd uur over alle STS'en |
| `moves_per_hour_<STS-id>` | idem per STS-kraan (bijv. `moves_per_hour_STS-01`) |
| `agv_wait_sts_s_mean` | gemiddelde wachttijd van een AGV in de STS-lane: van aankomst (TRANSPORT naar de STS voltooid) tot de kraanjob (DISCHARGE/LOAD) van die order voltooid is (s) |
| `agv_wait_tp_s_mean` | alleen LOAD: gemiddelde wachttijd van een AGV aan het TP, van aankomst (lege TRANSPORT naar het TP voltooid) tot de toewijzing van de volgende TRANSPORT van die AGV (de rit met de box naar de STS) (s) |
| `agv_travel_s_mean` | gemiddelde duur van een TRANSPORT-job, van toewijzing tot voltooiing (s) |
| `utilisation_STS` / `_ASC` | aandeel samples waarin het apparaattype BUSY was |
| `utilisation_AGV` | aandeel samples (AGV × sample) waarin een AGV BUSY was **of** door de dispatcher aan een order gekoppeld (bijv. wachtend in de lane) |
| `vessel_turnaround_s` | tijd van de eerste tot de laatste voltooide STS-move (DISCHARGE/LOAD) (s) |
| `faults` | aantal opgetreden storingen (in-process: teller van de sim; OPC UA: aantal `STATE_CHANGED → Fault`-events, zie OPC UA-modus) |
| `replans` | aantal herplanningen door de dispatcher |
| `energy_kwh_per_move` | energieverbruik per STS-move (kWh) |
| `containers_done` | aantal afgeronde containers |
| `containers_failed` | aantal mislukte containers |
| `containers_stuck` | open orders die de watchdog na 5 annuleringen als vastgelopen markeerde (bij een geslaagde run 0) |
| `sim_elapsed_s` | werkelijk gesimuleerde tijd van de run (s) |
| `timed_out` | 1.0 als `duration_s` bereikt werd voordat alle orders klaar waren, anders 0.0 |

## Architectuur

Workspace-packages in `packages/`:

- `model` (`terminal_model`): gedeelde types, locaties, JSON-logging en de `EquipmentPort`-interface.
- `sim` (`terminal_sim`): layout, scenario, `World` met STS, AGV, ASC, stack en storingen.
- `ecs` (`terminal_ecs`): `Dispatcher` en werkorders; praat alleen met `EquipmentPort`.
- `tos_stub` (`terminal_tos_stub`): slottoewijzing en werkordergenerator; krijgt kale argumenten (blokmaten, STS-id's), geen sim-objecten.
- `runner` (`terminal_runner`): koppelt sim, ecs en tos_stub (vertaalt Layout/Scenario naar de tos_stub-argumenten, vult de stack voor LOAD-orders); KPI-collector en CLI.

- `opcua` (`terminal_opcua`): OPC UA-namespace, JSON-codecs en `OpcUaEquipmentPort` (Plan B1).
- `gateway` (`terminal_gateway`): aggregatie-gateway voor alle equipment-servers (Plan B1).

Importregel (bewaakt door `tests/test_architecture.py`): `model` importeert geen ander `terminal_*`-package; `opcua` alleen `model` (plus `asyncua`); `sim` en `gateway` alleen `model` en `opcua`; `ecs` importeert alleen `model`; `tos_stub` importeert alleen `model` en `ecs`. Alleen `runner` kent alle packages.

## OPC UA-modus (Plan B1)

Drie processen, elk met een eigen CLI (`--host` is overal het bindadres, default `0.0.0.0`):

| Proces | CLI | Poorten |
|---|---|---|
| sim | `uv run python -m terminal_sim.serve scenarios/baseline.yaml [--layout terminal.yaml] [--base-port 48400] [--host 0.0.0.0] [--autostart]` | equipment-servers op `base-port + i` (max. 12, `opc.tcp://host:poort/terminal/<id>`), klokserver op `base-port + 12` (`/terminal/control`) |
| gateway | `uv run python -m terminal_gateway.serve [--config terminal.yaml] [--port 4840] [--host 0.0.0.0] [--upstream-host H] [--base-port P] [--n-sts 2] [--n-agv 6]` | `4840`; `--upstream-host`/`--base-port` default uit `opcua.host`/`opcua.base_port` in `terminal.yaml`; `--n-sts`/`--n-agv` moeten bij het scenario van de sim passen |
| runner | `uv run python -m terminal_runner.run scenarios/baseline.yaml --port opcua [--gateway opc.tcp://localhost:4840] [--out out/]` | client |

Volgorde: sim, gateway, runner (de gateway herstelt vanzelf als hij vóór de sim start; de runner probeert 10× met 2 s ertussen en stopt anders met exitcode 1 en het endpoint in de melding). Sim en runner moeten **hetzelfde scenariobestand** gebruiken: de sim gebruikt de seed voor de apparaattijden, de runner voor de werkorders. Na het verbinden controleert de runner dat de gateway per type evenveel apparaten spiegelt als het scenario (`n_sts`, `n_agv`) en de layout (één ASC per blok); bij een verschil stopt hij met exitcode 1 en de melding welke `--n-sts`/`--n-agv` de gateway nodig heeft. `--replications` > 1 wordt in opcua-modus geweigerd (elke replicatie vraagt een vers sim-proces). Na afloop stuurt de runner `Stop` naar de klokserver en eindigt de sim netjes; de gateway stop je met SIGTERM/Ctrl-C.

- **`speed_factor`**: de sim loopt op wandklok, `speed_factor` sim-seconden per wandklokseconde (stap `SIM_TICK_S` = 1 s sim). De baseline (≈ 11 800 s sim) duurt bij 100 zo'n 2 minuten.
- **Gepauzeerde start**: de sim start gepauzeerd op sim-tijd 0 (log `waiting for Start`) en loopt pas als `Terminal/Clock.Start` wordt aangeroepen. De runner verbindt, plaatst de LOAD-containers met `PrefillYard` (dat de sim alleen op sim-tijd 0 accepteert), dient de werkorders in, doet de eerste dispatch en roept dan `Start` aan; KPI-tijden en -rates zijn relatief aan de sim-tijd bij `Start` (normaal 0). Met `--autostart` loopt de klok meteen (handig zonder runner); `PrefillYard` werkt dan niet meer, dus scenario's met LOAD-orders falen dan met een duidelijke melding.
- **Verbinding gateway ↔ equipment-server**: valt de verbinding weg, dan meldt de gateway het apparaat als `STATE_CHANGED → FAULT` (reason `UPSTREAM_DISCONNECTED`); na herstel volgt een `STATE_CHANGED` naar de actuele toestand van de server (reason `UPSTREAM_RECONNECTED`), zodat de ECS het apparaat weer inzet. Staat de server zelf op `FAULT`, dan volgt geen herstelevent. Of communicatieverlies als equipmentstoring moet gelden, is een beslissing voor B2/B3.
- **KPI `faults`**: de storingsteller van de sim is via OPC UA niet beschikbaar. In opcua-modus telt de runner daarom de `STATE_CHANGED → Fault`-events; daaronder vallen ook de Faults die de gateway synthetiseert als een equipment-server wegvalt.
- **Tests**: `uv run pytest -m e2e_opcua` start sim en gateway als subprocessen op vrije poorten en draait drie tests: 20 containers / 1 STS / 2 AGV's met 10 LOAD-orders (≈ 45 s), hetzelfde kleine scenario (alleen lossen) bij ×25 vergeleken met in-process (≈ 2 min) en de baseline bij ×100 vergeleken met in-process (≈ 3,5 min). Ze staan standaard uit (`addopts = "-m 'not e2e_opcua'"` in `pyproject.toml`) omdat de suite al ≈ 4 minuten duurt.
- **Afwijkingen van het ontwerp**: (1) KPI-tijden lopen vanaf de start van de run (de sim-tijd `t0` bij `Start`, `finish(now - t0)`), niet vanaf sim-tijd 0, zodat een sim met `--autostart` geen voorlooptijd meetelt. (2) De runner roept `kpi.finish()` aan vóór `Stop`, omdat de gateway gestopte servers als `Fault` meldt en die anders als storingen zouden meetellen. (3) Events worden elke ≥ 10 ms wandklok afgeleverd (`drain_events`) in plaats van één keer per sample-venster van 60 s, omdat de ECS vanuit zijn event-callback dispatcht en anders tot 60 s sim per overdracht zou achterlopen.
- **Pacing-latentie en toleranties**: tellingen (`containers_done/failed`, `replans`, `faults`) zijn via OPC UA exact gelijk aan in-process, maar de tijd-KPI's lopen structureel iets achter. Elke overdracht event → gateway → runner → `AssignJob` kost 50–100 ms wandklok; bij `speed_factor` 100 is dat 5–10 s sim per overdracht (plus afronding op de sim-tick van 1 s), en omdat de STS de bottleneck is telt dat over de hele run op. `test_baseline_opcua_matches_inprocess` (×100) staat daarom `moves_per_hour_total` binnen 10 % en `vessel_turnaround_s` binnen 900 s toe; `test_small_scenario_gap_shrinks_with_lower_speed` laat zien dat de afwijking bij ×25 krimpt (binnen 5 % en 120 s). Een tweede timingeffect: `SimTime` en de events komen over aparte verbindingen (klokserver resp. equipment-servers, elk via de gateway), dus de runner kan een tick op sim-tijd t uitvoeren voordat alle events ≤ t binnen zijn. Een step-on-demand-klok (de sim wacht op de runner in plaats van op de wandklok) neemt beide effecten weg; dat is gepland voor B2.

**Architectuur.** `terminal_sim.serve` draait de salabim-wereld en publiceert elk apparaat als eigen OPC UA-server, plus een klokserver met `SimTime`, `SpeedFactor`, `Start`, `Stop` en `PrefillYard`. `terminal_gateway` spiegelt al die servers onder één namespace (`Terminal/Quay|Fleet|Yard|Clock`), stuurt methodes door en meldt een weggevallen server als `Fault`. De runner praat alleen met de gateway via `OpcUaEquipmentPort`, dat het `EquipmentPort`-protocol implementeert; events worden in de runner-thread afgeleverd (`drain_events`, elke ≥ 10 ms wandklok). De ECS (`terminal_ecs`) is daarvoor niet gewijzigd: `test_ecs_unchanged_since_plan_a` vergelijkt een sha256 over de ECS-bronnen met de waarde van `master` in `tests/fixtures/ecs_sources.sha256`.

### Equipment-events (OPC UA Events, Plan B3)

Equipment-events (`STATE_CHANGED`, `JOB_COMPLETED`, `JOB_FAILED`) zijn echte OPC UA Events met eigen EventTypes (namespace `urn:terminal:model`); ze vervangen de JSON-variabelen `LastEvent`/`EventSeq` uit B1. De sim-servers zenden ze uit op het `Server`-object met het equipment-object als `SourceNode` (asyncua levert events alleen aan abonnees van het uitzendende node en volgt `HasNotifier` niet), dus abonneer je, ook in UaExpert, op het `Server`-object. De gateway stuurt ze door met een eigen `Seq`, en `OpcUaEquipmentPort` abonneert zich erop.

| Type | Supertype | Eigen velden |
|---|---|---|
| `TerminalEventType` | `BaseEventType` | `EquipmentId` String, `Seq` UInt64, `SimTime` Double, `JobId` String, `Reason` String, `NewState` String |
| `StateChangedEventType` | `TerminalEventType` | n.v.t. |
| `JobCompletedEventType` | `TerminalEventType` | n.v.t. |
| `JobFailedEventType` | `TerminalEventType` | n.v.t. |

Een lege string in `JobId`/`Reason`/`NewState` betekent `None`. `Seq` is een teller per apparaat, begint bij 1 na start van de uitzendende server en is strikt oplopend; hij dient alleen voor gapdetectie (een gat wordt als WARNING gelogd). Events die upstream worden uitgezonden terwijl de verbinding gateway-sim weg is, gaan verloren en verschijnen na herstel als `Seq`-gat in het log; de snapshot blijft leidend voor de toestand.

Voorbeeld met een eigen asyncua-client (abonneren op alle events van een server):

```python
from asyncua import Client
from terminal_opcua.events import resolve_event_types, subscribe_terminal_events, event_from_ua

class Handler:
    def __init__(self, types): self.types = types
    def event_notification(self, event): print(event_from_ua(event, self.types))  # (seq, EquipmentEvent)

async with Client("opc.tcp://localhost:4840") as client:
    types = await resolve_event_types(client)
    sub = await client.create_subscription(100, Handler(types))
    await subscribe_terminal_events(client, sub, types)
```

## ECS als service (Plan B2a)

De ECS draait als eigen proces dat via de gateway met de sim praat en een HTTP/WebSocket-API biedt.

| Proces | CLI | Rol |
|---|---|---|
| ecs-service | `python -m terminal_ecs_service.serve --gateway opc.tcp://localhost:4840 --layout terminal.yaml --host 0.0.0.0 --port 8000` | Dispatcher + KPI + API |
| tos-stub | `python -m terminal_tos_stub.submit scenario.yaml --layout terminal.yaml --ecs http://localhost:8000` | genereert orders uit het scenario (seed) en POST ze naar de service (alleen dat: zie hieronder) |
| runner | `python -m terminal_runner.run scenario.yaml --port http --ecs http://localhost:8000 --gateway opc.tcp://localhost:4840 --out out/` | orkestreert één run (health, `PrefillYard`, orders, `Start`, pollen, KPI's, `Stop`) |

Alle tijden in sim-seconden.

`terminal_tos_stub.submit` POST alleen de orders: hij plaatst geen LOAD-containers in de yard (`PrefillYard`) en start de sim niet (`Start`). LOAD-orders lopen dan vast op `SLOT_EMPTY`. Gebruik voor een volledige run de runner met `--port http`; `submit` los alleen met scenario's die uitsluitend DISCHARGE-orders hebben (`discharge_share: 1.0`), en start de sim dan zelf.

Eén ECS-proces per sim-run: de service herkent een herstart van de sim niet als nieuwe run (Dispatcher, orders en KPI's blijven van de vorige). Gaat de sim-tijd terug, dan logt de service een ERROR en antwoordt `/health` 503 met `"error": "sim_time_reversed"`; de runner (`--port http`) weigert een service die al orders heeft (`done + open + failed > 0`). Herstart de service dus per run, totdat B2c herstartgedrag vastlegt.

| Endpoint | Antwoord |
|---|---|
| `POST /workorders` body `[{"container_id","type":"DISCHARGE"\|"LOAD","sts_id","yard_slot"}]` | 202 `{"accepted": n}`; 422 `{"errors":[{"container_id","code","message"}]}` (hele batch afgewezen; codes `DUPLICATE_CONTAINER`, `UNKNOWN_STS`, `INVALID_SLOT` en `INVALID_ORDER` voor een misvormde body); 503 als niet verbonden |
| `GET /status` | `Dispatcher.status()` + `sim_time`, `connected`, `uptime_s`, `tick_count` |
| `GET /kpis` | KPI-snapshot (zelfde sleutels als `finish`, incl. `sim_elapsed_s`), niet-destructief |
| `GET /equipment` | `[{"equipment_id","kind","vendor","state","position":{"x","y"},"current_job_id","energy_kwh","extra"}]` |
| `GET /health` | 200 `{"connected": true, "sim_time": t}`; 503 `{"connected": false, "sim_time": null}`, of 503 `{"connected": true, "sim_time": t, "error": "sim_time_reversed"}` na een sim-herstart |
| `WS /events` | JSON-berichten, zie hieronder |

Berichten op `WS /events` (veld `type`, geen replay):

| `type` | Wanneer |
|---|---|
| `status` | direct na verbinding en na elke tick (`sim_time` + `Dispatcher.status()`) |
| `equipment` | elk equipment-event (`kind`, `equipment_id`, `sim_time`, `job_id`, `reason`, `new_state`) |
| `job` | elke nieuw toegewezen job, na elke tick en na `POST /workorders` (`job_id`, `equipment_id`, `sim_time` = moment van toewijzing, `assigned_at`) |
| `link` | bij verlies en herstel van de gatewayverbinding (`connected`, `sim_time`) |

Per client geldt een wachtrij van 1000 berichten; bij vol worden berichten gedropt.

Simulatiebesturing (`PrefillYard`, `Start`, `Stop`) blijft bij de runner, via OPC UA op de gateway; de service heeft er geen endpoints voor.

Verbindingsverlies: de service houdt zijn Dispatcher en orders vast, verbindt elke 2 s opnieuw met de gateway, stuurt `link`-berichten bij verlies en herstel, en antwoordt 503 (`/health`, `POST /workorders`) zolang hij niet verbonden is. Events van de gateway tijdens de onderbreking gaan verloren; hoe de ECS daarna reconcilieert staat in de volgende sectie (Plan B2d).

## Storingen, verbindingsverlies en vrijgave (Plan B2d)

Ontwerp: `docs/superpowers/specs/2026-11-10-plan-b2d-link-loss-design.md`.

**Toestanden** van een apparaat (`State` in OPC UA, `state` in `GET /equipment`):

| Toestand | Betekenis | Dispatcher |
|---|---|---|
| `IDLE` | beschikbaar | krijgt jobs (tenzij het nog vrijgegeven moet worden) |
| `BUSY` | voert een job uit | — |
| `FAULT` | storing gemeld door het apparaat | lopende job wordt herpland; apparaat uitgesloten en `needs_release` |
| `OFFLINE` | verbinding weg (gateway↔apparaat: reden `UPSTREAM_DISCONNECTED`; ECS↔gateway: `LINK_LOST`) | **geen** herplanning, order blijft in zijn stage, geen nieuwe jobs, watchdog bevroren |

Komt een apparaat terug (`UPSTREAM_RECONNECTED` / `LINK_RESTORED`) als `BUSY`, dan loopt de job gewoon door. Komt het terug als `IDLE` terwijl er een job liep waarvan geen `JOB_COMPLETED`/`JOB_FAILED` binnenkwam, dan is de uitkomst **onbekend**. Terug als `FAULT`: zoals hierboven.

**Alarmen** (`GET /alarms`, gesorteerd op `since_sim_time`; WS-bericht `{"type":"alarm","action":"raised"|"cleared",…}`):

| `kind` | Wanneer | Opgeheven door |
|---|---|---|
| `LINK_LOST` | per apparaat dat OFFLINE is | herstel van de verbinding |
| `NEEDS_RELEASE` | na een FAULT of een onbekende uitkomst | `POST /equipment/{id}/release` |
| `UNKNOWN_OUTCOME` | job liep tijdens OFFLINE, apparaat kwam IDLE terug | `POST /jobs/{job_id}/resolve` |

`GET /status` telt mee: `offline`, `needs_release`, `unknown_outcomes`.

**Endpoints voor de operator:**

```bash
curl -s http://localhost:8000/alarms
# [{"kind":"UNKNOWN_OUTCOME","equipment_id":"STS-01","job_id":"C0008-DISCHARGE-1","since_sim_time":1003.0,"detail":"…"}, …]

curl -s -X POST http://localhost:8000/jobs/C0008-DISCHARGE-1/resolve \
     -H 'content-type: application/json' -d '{"outcome":"completed"}'
# 200 {"job_id":"C0008-DISCHARGE-1","outcome":"completed"}; 404 geen onbekende uitkomst; 422 ongeldige outcome; 503 niet verbonden

curl -s -X POST http://localhost:8000/equipment/STS-01/release
# 200 {"released":"STS-01"}; 404 onbekend; 409 {"code":"RELEASE_REFUSED","message":…}; 503 niet verbonden
```

Een order waarvan de jobs 5 keer achter elkaar mislukken (JOB_FAILED of FAULT, zonder voltooide job ertussen) wordt geparkeerd als `stuck` (`GET /status`, KPI `containers_stuck`, ERROR in de log): de ECS geeft hem niet opnieuw uit, zodat een direct falende job niet eindeloos herhaald wordt.

`release` wordt geweigerd (409) zolang het apparaat `FAULT` of `OFFLINE` meldt of er voor dat apparaat nog een onbekende uitkomst openstaat (`"resolve job <id> first"`).

**Wat doet de operator?**

- *Na een FAULT:* de job is al herpland. Controleer het apparaat ter plaatse; zodra het weer `IDLE` meldt: `POST /equipment/{id}/release`. Zonder vrijgave krijgt het geen jobs meer.
- *Na een onbekende uitkomst:* kijk wat er werkelijk gebeurd is (staat de container op de AGV / in de stack / nog onder de kraan?). Gebeurd → `resolve` met `"completed"` (de order gaat door alsof `JOB_COMPLETED` binnenkwam); niet gebeurd → `"failed"` (de order wordt herpland). Daarna `release` van het apparaat.

**Onbemande runs:** `terminal_ecs_service.serve --auto-release` (standaard uit) geeft een apparaat na FAULT → IDLE direct weer vrij en telt een onbekende uitkomst direct als `completed` (geen alarm): bij OFFLINE werkt het apparaat door, dus een job die al liep kan alleen voltooien of in een FAULT eindigen (en dan meldt het apparaat zich FAULT, niet IDLE). Zonder `--auto-release` beslist de operator via `resolve`. De runner draait in-process standaard met auto-release; `terminal_runner.run --no-auto-release` schakelt het uit (dan blijven FAULT-apparaten uitgesloten en onbekende uitkomsten open). Met `--port http` heeft die vlag geen effect: daar beslist de service (`--auto-release`).

## Verder

Ontwerp: `docs/superpowers/specs/2026-11-10-terminal-platform-fase1-design.md` en `docs/superpowers/specs/2026-11-10-plan-b1-opcua-design.md`. Plan B2 (ECS-service, viewer, Docker) en B3 (eigen DataTypes, beveiliging, vendor-adapters) volgen.
