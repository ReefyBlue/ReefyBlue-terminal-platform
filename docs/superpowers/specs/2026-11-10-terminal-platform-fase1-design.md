# Terminal Platform — Fase 1 ontwerp

Datum: 2026-11-10
Status: ter beoordeling

## 1. Doel en scope

Een eigen, merkonafhankelijk besturingsplatform voor een volautomatische containerterminal, gebouwd op OPC UA, met een discrete-event simulator als testbank. Fase 1 is een vereenvoudigde fictieve terminal. Als die werkt, wordt de gesimuleerde apparatuur vervangen door vendor-adapters voor een echte terminal.

**In scope fase 1**
- 1 ligplaats, 2 STS-kranen, 6 AGV's, 4 stackblokken met elk 1 ASC
- Stroom: schip → STS → AGV → ASC → stack (lossen) en omgekeerd (laden)
- OPC UA-informatiemodel, gesimuleerde equipment-servers, gateway, ECS-dispatch, TOS-stub, webviewer, scenario-runner met KPI's

**Buiten scope fase 1**
- Gate, spoor, reefers, gevaarlijke lading, douane
- Vendor-adapters, beveiliging/authenticatie op OPC UA (anonymous, lokaal netwerk)
- 3D-weergave (QuaySim/Godot komt later als viewer)

**Succescriteria**
- Een lossing van 200 containers loopt van begin tot eind zonder handmatig ingrijpen
- Dezelfde seed geeft exact dezelfde uitkomst (reproduceerbaar)
- Een scenario (bijv. 4 vs 6 AGV's) levert vergelijkbare KPI's op in één rapport
- De ECS bevat geen enkele verwijzing naar de simulator; hij praat alleen met de gateway

## 2. Architectuur

```
TOS-stub ──API──▶ ECS ──OPC UA──▶ Gateway ──OPC UA──▶ 12 equipment-servers
                   │                                        │
              websocket                                Sim-engine (Salabim)
                   │
               Webviewer
```

De viewer hangt aan de ECS, niet aan de simulator. De ECS stuurt door wat hij via de gateway ziet (posities, states, jobs) plus zijn KPI's. Zo werkt dezelfde viewer later ook op een echte terminal.

| Component | Proces | Taak |
|---|---|---|
| `sim` | 1 proces | Salabim-wereld + alle equipment-gedrag + 12 asyncua-servers (één gedeelde klok) |
| `gateway` | 1 proces | asyncua-server die 12 servers aggregeert onder één namespace |
| `ecs` | 1 proces | asyncua-client op de gateway; dispatch en jobbewaking; FastAPI voor TOS en viewer |
| `tos-stub` | 1 proces | levert werkorders (los/laadlijst) via HTTP; later vervangen door de eigen TOS-simulatie |
| `viewer` | static + ws | browser-canvas met animatie en KPI-panelen |

Alles via Docker Compose. Ontwikkeling lokaal met `uv`, later op de NAS.

**Klok.** De sim-engine is de enige bron van tijd. Snelheidsfactor instelbaar (1× voor demo, tot 100× voor scenario's). De ECS is event-gedreven en kent geen wandkloktijd; timeouts worden uitgedrukt in sim-tijd die de gateway publiceert op `Terminal/Clock/SimTime`.

## 3. Informatiemodel (OPC UA namespace `urn:terminal:model`)

**`EquipmentType`** (abstract)
- Variabelen: `Id`, `Kind` (STS/AGV/ASC), `Vendor`, `State` (Idle/Busy/Fault/Maintenance), `Position` (x,y m), `CurrentJobId`, `Energy` (kWh)
- Methods: `AssignJob(Job) → bool`, `CancelJob() → bool`, `Reset() → bool`
- Events: `StateChanged`, `JobCompleted(JobId)`, `JobFailed(JobId, Reason)`

**`STSType`**: `BerthSide`, `SpreaderHeight`, `TrolleyPosition`, `HandoverLaneOccupied`
**`AGVType`**: `Battery` (%), `LoadedContainerId`, `Route` (waypoints), `Speed`
**`ASCType`**: `BlockId`, `GantryPosition`, `WaterSideTPOccupied`, `LandSideTPOccupied`

**`JobDataType`** (structure): `JobId`, `Type` (DISCHARGE/LOAD/STACK_IN/STACK_OUT/TRANSPORT), `ContainerId`, `From`, `To`, `Priority`, `Deadline`, `Status` (Assigned/Running/Done/Failed)

**Locatie-notatie** (string): `STS-01`, `AGV-03`, `A/12/3/2` (blok/bay/row/tier), `TP/A/WS` (transfer point blok A waterzijde)

**Namespace-boom in de gateway**
```
Terminal/
  Clock/SimTime, SpeedFactor
  Quay/STS-01, STS-02
  Fleet/AGV-01 … AGV-06
  Yard/Block-A/ASC-A … Block-D/ASC-D
  Jobs/<JobId>            (actieve jobs, verwijderd na Done/Failed)
```

Vendor-adapters later: mappen vendor-variabelen op dit model; extra's onder `VendorSpecific/` per object.

## 4. Simulatiewereld

**Layout** (meters, oorsprong = kade-hoek waterzijde)
- Kade langs x-as, 400 m. STS-01 op x=100, STS-02 op x=250, elk met overslagbaan voor 2 AGV's
- AGV-gebied: rechthoek 400 × 120 m tussen kade en stack, rijbanen als grid (20 m raster), eenrichting per baan
- Stack: 4 blokken van 40 bays × 6 rows × 4 tiers, haaks op de kade, elk met één transferpunt waterzijde (TP-WS) op y=120
- Geen landzijde-activiteit in fase 1 (TP-LS bestaat in het model, blijft ongebruikt)

**Apparatuurgedrag** (deterministisch met optionele stochastiek per seed)
- STS: cyclustijd per move ~ triangular(90, 110, 150) s; wacht op AGV in baan; één container per cyclus
- AGV: 6 m/s leeg, 4 m/s beladen; routing via kortste pad op het grid; kruispuntreservering (één AGV per cel); batterij daalt 0,02 %/m, laden niet in fase 1 (batterij alleen gemeten)
- ASC: gantry 4 m/s, trolley 1,5 m/s, hijs 1 m/s; put-away kiest laagste vrije tier in doel-bay; één container per cyclus

**Storingen** (scenario-instelling): per apparaattype een MTBF/MTTR; bij storing gaat `State → Fault`, lopende job → `Failed`, ECS herplant.

**Containers**: `ContainerId`, lengte (20/40), gewicht, doel-blok (toegewezen door TOS-stub). Geen twin-lift in fase 1.

## 5. ECS — dispatchlogica

Simpele, uitlegbare regels; geen optimalisatie in fase 1.

1. **Werkorder-intake**: TOS-stub levert een lijst `{containerId, type, stsId, yardSlot}` in kraanvolgorde.
2. **Lossen** (`DISCHARGE`): per STS een FIFO-wachtrij. Als STS klaar is voor de volgende container en er is een vrije AGV, dan:
   - kies dichtstbijzijnde `Idle` AGV (euclidisch), `AssignJob(TRANSPORT: STS → TP/<blok>/WS)`
   - STS `AssignJob(DISCHARGE)` zodra AGV in baan staat
   - bij aankomst AGV op TP: ASC `AssignJob(STACK_IN: TP → slot)`; AGV wordt vrij na overdracht
3. **Laden** (`LOAD`): spiegelbeeld; ASC `STACK_OUT` eerst, AGV ophalen bij TP, STS `LOAD` als AGV in baan.
4. **Lookahead**: ECS houdt per STS maximaal 2 AGV's onderweg (voorkomt rij in de baan).
5. **Herplanning**: bij `JobFailed` of `Fault` → job terug in wachtrij, apparaat uitgesloten tot `Idle`.
6. **Deadlocks**: watchdog in sim-tijd; als een AGV > 10 min geen vooruitgang boekt → `CancelJob` en herroute.

ECS-interne state: in-memory, herbouwbaar vanuit gateway (geen database in fase 1).

**ECS HTTP-API** (FastAPI)
- `POST /workorders` — lijst werkorders (vanuit TOS-stub)
- `GET /status` — samenvatting per apparaat en openstaande jobs
- `GET /kpi` — lopende KPI's
- `WS /events` — eventstroom voor de viewer

## 6. TOS-stub

Genereert een los- en laadlijst voor één schip op basis van een scenario-bestand (YAML): aantal containers, verdeling 20/40, doelblokken (round-robin of gewogen). Post dit naar de ECS. Later vervangen door de eigen TOS-simulatie, die dezelfde endpoint gebruikt.

## 7. Gateway

- asyncua-server die bij opstart `terminal.yaml` leest (lijst equipment met endpoint-URL en type)
- Per equipment: subscribe op alle variabelen en events van de onderliggende server; spiegelt ze in eigen namespace; method-calls worden doorgestuurd
- Equipment onbereikbaar → `State = Fault`, reconnect elke 5 s
- Geen caching van jobs; `Jobs/` is een view op wat de ECS heeft toegewezen

Dit is bewust dun: de gateway bevat geen logica, alleen aggregatie en vertaling. Daar komen later de vendor-adapters in.

## 8. Webviewer

- Statische pagina (Canvas 2D) + websocket naar `ecs /events`
- Tekent layout, apparatuur met kleur per `State`, containers op AGV's, stackbezetting per blok als heatmap
- Panelen: moves/uur per STS, gemiddelde AGV-wachttijd in baan, AGV-bezetting %, ASC-bezetting %, openstaande jobs, sim-tijd en snelheidsfactor
- Bediening: start/pauze, snelheidsfactor, scenario kiezen. Verder alleen lezen.

## 9. Scenario's en KPI's

**Scenario-bestand** (`scenarios/*.yaml`): seed, aantal AGV's, aantal STS, containers, storingsparameters, snelheidsfactor, duur.

**Runner** (`python -m sim.run scenario.yaml --replications 10`): draait N replicaties met oplopende seed, schrijft per run een CSV met KPI's en een samenvatting (gemiddelde, p5/p95).

**KPI's**: moves/uur per STS en totaal; AGV-wachttijd bij STS en TP; AGV-reistijd per job; bezettingsgraad per apparaattype; doorlooptijd van het schip; aantal storingen en herplanningen; energie per move.

## 10. Datastromen

Lossen van één container, gelukkig pad:
1. TOS-stub → ECS: werkorder
2. ECS → Gateway → AGV-03: `AssignJob(TRANSPORT)`; AGV rijdt naar STS-01 baan
3. AGV-03 event `JobCompleted` (aangekomen); ECS → STS-01: `AssignJob(DISCHARGE)`
4. STS-01 `JobCompleted`; `AGV-03.LoadedContainerId` gezet; ECS → AGV-03: `AssignJob(TRANSPORT → TP/B/WS)`
5. AGV-03 `JobCompleted`; ECS → ASC-B: `AssignJob(STACK_IN)`
6. ASC-B `JobCompleted`; AGV-03 `Idle`; container staat in `B/12/3/1`
7. ECS → viewer: elke statuswijziging uit de gateway als event, plus KPI-update

## 11. Foutafhandeling

| Situatie | Gedrag |
|---|---|
| Equipment server weg | Gateway zet `Fault`; ECS sluit apparaat uit; jobs herpland |
| `AssignJob` geweigerd (apparaat niet `Idle`) | ECS logt en probeert volgend apparaat |
| Job time-out (sim-tijd) | `CancelJob`, apparaat `Reset`, job terug in wachtrij, teller `replans++` |
| Stackslot bezet bij `STACK_IN` | ASC geeft `JobFailed(SLOT_OCCUPIED)`; ECS vraagt nieuw slot aan TOS-stub |
| AGV-deadlock op grid | Watchdog; één AGV wordt teruggezet naar laatst vrije cel en herrouteert |
| Ongeldige werkorder | ECS weigert met 422 en reden |

Alle fouten gaan als gestructureerde logregels (JSON) naar stdout en als events naar de viewer.

## 12. Testen

- **Unit**: dispatchregels (keuze AGV, lookahead-limiet, herplanning), routing op grid, stack-slotkeuze, informatiemodel-serialisatie
- **Integratie**: sim + gateway + ECS in-process met 1 STS, 2 AGV's, 1 ASC; lossing van 20 containers eindigt met alle containers in de stack
- **Reproduceerbaarheid**: twee runs met dezelfde seed geven identieke KPI-CSV
- **Contract**: de ECS-tests draaien tegen een *fake* gateway (in-memory) om te bewijzen dat de ECS niets van de simulator weet
- **End-to-end**: `docker compose up` + scenario `baseline.yaml` → KPI-rapport zonder fouten

## 13. Repo-structuur

```
terminal-platform/
  pyproject.toml            (uv workspace)
  docker-compose.yml
  terminal.yaml             (layout + equipment-registratie)
  scenarios/baseline.yaml, agv4.yaml, faults.yaml
  packages/
    model/      informatiemodel: typen, Job, locaties, nodeset-generator
    sim/        Salabim-wereld, equipment-gedrag, asyncua-servers, runner
    gateway/    aggregatieserver
    ecs/        dispatch, API, websocket
    tos_stub/   werkorder-generator
    viewer/     static html/js
  tests/
  docs/superpowers/specs/
```

`model` is de enige gedeelde afhankelijkheid; `ecs` importeert nooit uit `sim`.

## 14. Pad naar fase 2

1. Gate en spoor in sim + model (`TruckType`, `RMGType`), landzijde-TP in gebruik
2. Eigen TOS-simulatie vervangt `tos_stub` op dezelfde endpoint
3. Eerste vendor-adapter (bijv. één echte of gedetailleerd gesimuleerde kraan) naast de sim-servers achter dezelfde gateway
4. Godot/QuaySim als alternatieve viewer op dezelfde eventstroom
5. OPC UA-beveiliging (certificaten, gebruikers) voordat iets een echt netwerk raakt
