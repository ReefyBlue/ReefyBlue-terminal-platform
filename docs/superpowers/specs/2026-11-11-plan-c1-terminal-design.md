# Plan C1 — Gedetailleerd terminalontwerp: layout en equipment op schaal (ontwerp)

Datum: 2026-11-11 · Status: ontwerp, ter review door de gebruiker

## 1. Doel

De fictieve terminal gaat van een schematisch 20 m-grid met vier kleine blokken naar een **maatvast ontwerp zoals een Konecranes/Gottwald-terminal**: Lift-AGV's met transferracks, twee ASC's per blok, een apron met STS-backreach, echte afmetingen en echte kinematica (versnellen, rijden, afremmen; hijsen, trolley, gantry) voor alle equipment. De simulator wordt daardoor voorspellend op het niveau van cyclustijden en productiviteit (moves/uur per STS), niet alleen op procesvolgorde.

Uitgangspunt is de **gebruiker-gekozen variant**: Lift-AGV met racks (ontkoppeld), scope **waterzijde + yard**; landzijde is een vaste in-/uitstroom aan de landzijdekop van elk blok zonder eigen equipment-model (volgt later, Plan C4).

Succescriteria:
1. `terminal.yaml` beschrijft de terminal volledig in meters (geen gridcellen meer als primaire eenheid); de bestaande `load_layout`-API blijft bestaan als afgeleide (het 20 m-grid wordt berekend uit de meter-layout voor de huidige AGV-router) tot Plan C2 de router vervangt.
2. Elk equipment heeft een **kinematisch profiel** (max. snelheid, versnelling, vertraging; voor kranen per as) met Konecranes-ordegrootte defaults; cyclustijden komen uit de bewegingen, niet uit een getrokken driehoeksverdeling.
3. STS-productiviteit in de baseline-run ligt tussen 25 en 35 moves/uur per kraan bij 5 Lift-AGV's per kraan (ordegrootte van echte ASC/AGV-terminals); de nieuwe KPI `sts_moves_per_hour` staat in `/kpis`.
4. Alle bestaande tests blijven groen op de **oude layout** (`terminal.yaml` → `terminal_legacy.yaml`, fixtures ongewijzigd); de nieuwe layout is `terminal_mv.yaml` en heeft eigen baseline-fixtures.
5. De viewer tekent de nieuwe layout op schaal uit `/layout` (Plan C3, apart).

Buiten scope (geparkeerd): landzijde-equipment (landzijde-ASC-cyclus, truck-lanes, gate, spoor) → C4; dubbele-trolley-STS met lashing platform → later; batterijladen/laadstations voor AGV's → C2 (routering) omdat laadlocaties routeringsdoelen zijn; windstilstand en weerseffecten; meerdere schepen tegelijk.

## 2. Referentiematen (publieke specificaties)

| Onderdeel | Waarde | Bron |
|---|---|---|
| STS (Konecranes STS, monobox) | railspan 30,0 m; trolley 180–244 m/min; hijs 75–90 laden / 150–180 leeg m/min; gantry 45 m/min; hefhoogte 36–40 m; outreach 22–24 rijen; 50–65 t | Konecranes STS tech. spec |
| Apron | 120–130 m van landzijde-STS-rail tot eerste yard-container; AGV's in de backreach achter een hek; 3 transfer-lanes per STS | Portwise/PTI 134 |
| Lift-AGV (Gottwald) | 14,8 × 3,0 m; laadvlak 2,4 m hoog; 6 m/s recht, 3 m/s in bochten, 1 m/s crab; 40 t enkel / 70 t 2×20'; ±25 mm; batterij-elektrisch | Konecranes Lift AGV datasheet |
| ASC (Konecranes RMG-klasse) | gantry 140–150 m/min; trolley 70 m/min; hijs 45 laden / 90 leeg m/min; 40–50 t; hefhoogte tot 21 m | Konecranes RMG |
| ASC-blok (Kuenz CTA/CTB, Kalmar) | 10 wide × 1-over-5, spoorbreedte ~31–40 m, kraanbaan 305–385 m | Kuenz, Kalmar |
| ASC-paar | waterzijde-ASC bedient de transferzone, landzijde-ASC de truckkop; op één spoor kunnen ze niet passeren | US 7,972,102 (Automated marine container terminal) |
| Onderhoudsgang tussen blokken; reeferaansluitingen landzijde | | idem |

Containermaten: 20' = 6,058 m, 40' = 12,192 m, breedte 2,438 m, hoogte 2,591 m (8'6") / 2,896 m (HC). Bay-pitch 6,5 m (20'), rij-pitch 2,9 m, tier 2,9 m (gemengd HC: rekenen met 2,75 m gemiddeld is níét toegestaan — de layout kiest één tierhoogte van 2,9 m).

## 3. De nieuwe terminal (`terminal_mv.yaml`)

Fictief, maar in verhouding tot een middelgrote ASC-terminal:

- **Kade**: 1 200 m, 2 ligplaatsen (A: 0–600, B: 600–1 200), kaderand y = 0; STS-rails op y = 10 (waterzijde) en y = 40 (landzijde, span 30 m).
- **STS**: 8 kranen (`STS-01`…`STS-08`), startposities op 75 + 150·n m; gantry-bereik per ligplaats; minimale onderlinge afstand 27 m (breedte over bumpers) → `min_gap_m`.
- **Apron**: y = 40 … 165 (125 m): STS-backreach met per STS **3 transfer-lanes** (lane-breedte 3,5 m, lengte 16 m) op y = 45–61; AGV-rijbanen: 4 banen parallel aan de kade (y = 70, 76, 82, 88; richting afwisselend oost/west), dwarsverbindingen elke 50 m; AGV-wachtvakken ("buffer") per STS 2 stuks op y = 95.
- **Transferzone waterzijde**: y = 110 … 160 vóór elke blokkop: per blok **5 rackposities** (20'/40'-rack op 2,4 m hoogte, pitch 3,5 m) in de lijn van het blok, plus 1 reefer/exception-positie; AGV nadert via een inrijbaan, zet af, rijdt leeg door (ontkoppeld).
- **Yard**: y = 165 … 525: **16 blokken** (`A`…`P`), hartafstand 42 m (10 wide × 2,9 m = 29 m + ASC-poot + onderhoudsgang 13 m), elk blok **40 bays × 10 rijen × 1-over-5** (tiers 5; 2 000 TEU-grondslots), kraanbaanlengte 360 m; twee ASC's per blok op één spoor (`ASC-A-WS`, `ASC-A-LS`), waterzijde-ASC werkt bay 1–40 + transferzone, landzijde-ASC bay 1–40 + truckkop; **passeren onmogelijk**, minimale afstand 2 bays → `asc_min_gap_bays: 2`; werkverdeling: WS-ASC doet alle waterzijde-jobs, LS-ASC alle landzijde-jobs en housekeeping; bij conflict wijkt de LS-ASC.
- **Landzijde**: y = 525 … 560 truckkop per blok met 4 truck-lanes (alleen als in-/uitstroombron: orders `TRUCK_IN`/`TRUCK_OUT` worden door de landzijde-ASC afgehandeld met een vaste cyclustijd; geen truck-model) — Plan C4 vult dit in.
- **Hekwerk**: apron+transferzone+yard zijn automatiseringsgebied; alleen de truckkop is bemand gebied (relevant voor noodstop-/safety-events in C2).

### 3.1 Terminalzones (volledigheid naar de Quaysim-tekening van de gebruiker)

De tekening `docs/superpowers/references/c1-terminal/quaysim_stack_layout_concept.pdf` (gebruiker, 2026) is de **checklist** voor wat een volautomatische terminal bevat; maatvoering en blokoriëntatie volgen §3 (niet de tekening). Alle zones staan als `zones:` in de layout (polygoon in meters, type, naam) zodat viewer en latere plannen ze kennen; in C1 hebben alleen de met ★ gemarkeerde zones gedrag.

| Zone | Type | Gedrag in C1 |
|---|---|---|
| ★ Berth area + kade | `berth` | STS-gantrybereik, scheepsbays |
| ★ AGV lanes (apron) | `agv_lanes` | rijbanen, kruisingen |
| ★ AGV parking area | `agv_parking` | parkeer-/wachtvakken; later laadstations (C2) |
| ★ Transferzones waterzijde (racks) | `transfer_ws` | rackposities |
| ★ Stackblokken | `block` | ASC-paar, stapelkolommen |
| ★ Interchange zone truck (per blokkolom) | `truck_interchange` | TRUCK_IN/OUT met vaste cyclustijd (C4: trucks) |
| Interchange zone rail + rail terminal | `rail_interchange`, `rail_terminal` | alleen zone (C4) |
| Gate in/out, OCR-gate, technical gate, customs, problem parking | `gate`, `ocr_gate`, `tech_gate`, `customs`, `problem_parking` | alleen zone (C4) |
| OOG & special cargo area | `oog` | alleen zone; OOG-orders worden in C1 geweigerd (`OOG_NOT_SUPPORTED`) |
| Reeferrijen in het blok | `reefer_rows` (attribuut van `Block`) | plug-slots: reefercontainers alleen daar (TOS-stub-regel) |
| TOB-hoofdgebouw, workshop/technical services | `building` | alleen zone; workshop = parkeerdoel voor equipment in MAINTENANCE (C2) |
| Onderhoudsgang tussen blokken | `maintenance_lane` | geen AGV-verkeer |

De demo-layout krijgt deze zones op plausibele plekken: railterminal en gate-complex aan de noordrand, OOG en workshop aan de oostrand, truck-interchange aan de landzijdekop van elke blokkolom.

Een `terminal_mv_small.yaml` (400 m kade, 3 STS, 4 blokken, 12 Lift-AGV's) is de **testlayout** voor snelle runs en de e2e-suites; de volledige `terminal_mv.yaml` is de demo/soak-layout.

## 4. Layoutmodel (`terminal_model.layout`, nieuw; `terminal_sim.layout` wordt een adapter)

```
Layout(name, quay: Quay(length_m, berths[Berth(id, from_m, to_m)]),
       sts_rails: Rails(y_ws_m, y_ls_m), sts: [StsSpec(id, x0_m, min_gap_m, berth_ids, lanes[Lane(id, x_m, y_m, len_m, width_m)])],
       apron: Apron(lanes[DriveLane(id, y_m, direction)], crossings_every_m, buffers[Buffer(id, x_m, y_m)]),
       transfer_zones: [TransferZone(block, racks[Rack(id, x_m, y_m, size: "20|40|45", reefer: bool)], approach_lane_id)],
       blocks: [Block(id, x0_m, y0_m, bays, rows, tiers, bay_pitch_m=6.5, row_pitch_m=2.9, tier_h_m=2.9, asc_ws_id, asc_ls_id, asc_min_gap_bays=2, maintenance_lane_side: "E|W")],
       landside: [TruckHead(block, lanes: int, y_m)],
       opcua: OpcUa(base_port, host))
```
- Alle coördinaten in meters, oorsprong = kade-west-hoek, x langs de kade, y landinwaarts.
- `Layout.grid(cell_m=20)` levert het **afgeleide grid** (cellen, STS-lane-cellen, TP-cellen = rackposities geprojecteerd) zodat de huidige `Grid`-router, dispatcher en viewer ongewijzigd blijven werken in C1. Het grid is een view, geen bron.
- Validatie bij laden: geen overlappende blokken/racks/lanes; STS-rails binnen de apron; racks binnen de transferzone van hun blok; `min_gap_m`/`asc_min_gap_bays` ≥ fysieke maat; elke STS heeft ≥ 1 lane; elk blok heeft 2 ASC-id's.
- `GET /layout` (ECS-service) serveert het nieuwe model (velden hierboven, plus `grid` als afgeleid blok voor compatibiliteit).

## 5. Equipmentmodellen (sim)

Gemeenschappelijk: `KinematicAxis(v_max, a, d)` met `travel_time(dist)` (trapezium/driehoek-profiel) en `position_at(t)`; elke beweging wordt gesimuleerd als één `hold` met tussentijdse positie-updates op `SAMPLE_DT = 0.5 s` sim-tijd voor de OPC UA-positie (viewer ziet vloeiende beweging). Alle snelheden komen uit de layout/`equipment.yaml` met Konecranes-defaults; `speed_factor` blijft werken.

### 5.1 STS
Assen: gantry (0,75 m/s; ±`min_gap_m` t.o.v. buren), trolley (3,5 m/s, a=0,8), hoist (laden 1,4 m/s, leeg 2,8 m/s, a=0,6). Scheepsmodel per ligplaats: `Vessel(bays[Bay(x_m, rows=22, tiers_on_deck=8, tiers_in_hold=10)])`; een move = trolley kade→bay-rij, hijs/daal in ruim of dek, terug naar de **transfer-lane** (y = 45–61), neerzetten op de Lift-AGV **of** — als de AGV er nog niet is — in de lane-buffer (één container per lane op de grond, `lane_buffer: true`, zoals bij backreach-operatie met racks óf grond). Twistlock-handling: vaste 12 s per move (`twistlock_s`) als placeholder voor het lashing-platform. STS-cyclus ≈ 90–130 s afhankelijk van bay/rij → 28–40 moves/u single-cycle; dual-cycling (discharge+load in één cyclus) als optie `dual_cycle: true` (default false in C1).

### 5.2 Lift-AGV
- Profiel: 6,0 m/s recht, 3,0 m/s bocht (cel met richtingverandering), 1,0 m/s in transferzone/lane (crab), a = 1,0, d = 1,2 m/s²; `length_m 14.8, width_m 3.0`.
- **Payload**: 1×20'/40'/45' of 2×20' (twee onafhankelijke platforms: `slots: [A, B]`); jobs kunnen één of twee TEU dragen (`twin_20` optie in orders; default uit in C1 maar model ondersteunt het).
- **Rackhandeling**: op rack aankomen → positioneren (8 s) → lift (12 s) → wegrijden; bij ophalen omgekeerd. De AGV wacht nooit op een ASC.
- **STS-lane**: AGV rijdt in, de STS zet/pakt (STS-cyclus bepaalt wachttijd); passieve overdracht, zoals in de praktijk.
- Batterij: verbruik per m en per lift; `charge_pct` zichtbaar; laden zelf is C2 (laadstations zijn routeringsdoelen).
- Routering in C1: huidige `Grid`-router op het afgeleide grid (richting van rijbanen wordt gerespecteerd via eenrichtings-edges in `Layout.grid()`); C2 vervangt dit door lane-gebaseerde routering met kruispuntreserveringen.

### 5.3 ASC (paar per blok)
- Assen: gantry 2,4 m/s (a=0,5), trolley 1,17 m/s, hoist 0,75 laden / 1,5 leeg m/s; `spreader_lock_s 6`.
- Twee kranen op één spoor met `asc_min_gap_bays`: `BlockTrack` arbitreert posities (reserveren van bay-ranges; de LS-ASC wijkt uit naar de landzijdekop als de WS-ASC een bay nabij de landzijde nodig heeft en vice versa — "claim/yield" met deadline).
- Waterzijde-cyclus: rack ↔ slot (pick op rack y=110–160 ligt binnen de kraanbaan; de ASC rijdt tot bay 0 = transferzone).
- Landzijde-ASC: in C1 alleen housekeeping-jobs (`STACK_MOVE` binnen het blok, voor digging) en `TRUCK_IN/OUT` met vaste cyclustijd (geen truck-model).
- Stapelregels: `Stack` bewaart per grondslot een kolom; **digging** (rehandles) wordt gemodelleerd: een LOAD van een container die niet bovenop ligt genereert `STACK_MOVE`-jobs voor de boxen erboven (de huidige "slot = één container" vereenvoudiging vervalt). Yard-planningsregel in de TOS-stub: discharge gaat naar het laagste van de laagste kolommen in het doelblok; load-sequentie volgt het stuwplan (bay-volgorde).

### 5.4 Events en OPC UA
Bestaande types blijven; nieuw: `JobType.STACK_MOVE`, `JobType.TRUCK_IN/TRUCK_OUT`; equipment-variabelen erbij: STS `TrolleyPos_m, HoistPos_m, LaneId`; AGV `Heading_deg, Speed_mps, LoadA, LoadB, ChargePct`; ASC `Bay, Row, Tier, TrolleyPos_m, Partner` (id van de andere kraan). Rack-bezetting als eigen OPC UA-object per transferzone (`TransferZone-A.Racks[i].ContainerId`) — de ECS leest de zone, niet de AGV, om te weten of een rack vrij is. Alles additief; bestaande clients breken niet.

## 6. Dispatcher/ECS (minimaal in C1, rest in C2)

C1 houdt de dispatcher-logica gelijk en breidt alleen uit wat nodig is om de nieuwe fysieke stappen te sturen:
- Nieuwe stagevolgorde discharge: `AWAIT_AGV → TO_STS_LANE → AT_STS (STS plaatst) → TO_RACK → ON_RACK → STACKING (WS-ASC) → STORED`. Load omgekeerd, plus voorafgaande `STACK_MOVE`-jobs bij digging.
- Rackkeuze: vrije rack van het doelblok; geen vrije rack → AGV wacht in buffer (`BUFFER_WAIT`) met herplanning naar een ander blok alleen voor discharge (TOS-stub geeft `alternative_blocks`).
- STS-lanekeuze: lane die het eerst vrij is; de STS houdt een `lane_buffer`-container aan als de AGV te laat is (geen STS-wachttijd zolang buffer vrij).
- Equivalentiefixtures: de oude layout + oude processen blijven exact (regressie); de nieuwe layout krijgt eigen baseline-fixtures (`mv_small_baseline_seed1.json`) die vanaf C1 worden vastgezet.

## 7. KPI's (nieuw in `terminal_kpi`)
`sts_moves_per_hour` (per STS en gemiddeld), `sts_wait_for_agv_s_mean`, `agv_utilisation_pct`, `agv_wait_rack_s_mean`, `asc_ws_utilisation_pct`, `rehandles_per_load`, `rack_occupancy_pct`. Bestaande KPI's blijven.

## 8. Opsplitsing en volgorde

| Plan | Inhoud | Resultaat |
|---|---|---|
| **C1a** | `terminal_model.layout` (meters) + validatie + `Layout.grid()` + `terminal_mv(.small).yaml` + `/layout` uitgebreid; oude yaml → `terminal_legacy.yaml`, alle tests groen | layout op schaal, nog oude equipment |
| **C1b** | Kinematica-module + STS/Lift-AGV/ASC-paar/racks/stack-met-kolommen in de sim; OPC UA-variabelen; dispatcher-stages §6; TOS-stub yardregels; nieuwe KPI's; baseline-fixtures `mv_small` | simulator op schaal |
| **C2** | Lane-routering met kruispunten, laadstations, STS-gantryverplaatsing tussen ligplaatsen, dual-cycling | realistisch verkeer |
| **C3** | Viewer: tekenen uit het metermodel (racks, lanes, twee ASC's, STS-trolley/hoist, scheepsbays), zoom/pan | zichtbaar |
| **C4** | Landzijde: truck-lanes, gate, LS-ASC-cyclus, eventueel spoor | compleet |

C1a en C1b worden als twee plannen uitgevoerd (SDD, aparte PR's op `feat/plan-b2c-deploy-soak`); daarna C3 voordat C2, zodat je de nieuwe terminal kunt zíen tijdens het verkeersmodel.

## 9. Beslissingen (door mij, zonder te vragen)

0. De Quaysim-tekening van de gebruiker is de volledigheidsreferentie (zones); blokoriëntatie en maten volgen de Konecranes-opzet (haaks op de kade) omdat die bij Lift-AGV's met racks hoort — de gebruiker gaf aan dat de tekening niet exact gevolgd hoeft te worden.

1. Fictieve maar realistische terminal (1 200 m, 8 STS, 16 blokken) als demo-layout; kleine variant voor tests. Geen kopie van een bestaande terminal.
2. Alle maten in meters; het 20 m-grid blijft tijdelijk als afgeleide view om C1 klein te houden; de router wordt in C2 vervangen.
3. Twee ASC's per blok op één spoor (niet-passerend), WS/LS-taakverdeling met claim/yield — de meest voorkomende configuratie.
4. Racks = 5 per blok + 1 exception-positie; AGV wacht nooit op een ASC (ontkoppeling is het hele punt van Lift-AGV's).
5. STS-lanes met grondbuffer (1 container) zodat de STS niet stilvalt als de AGV laat is.
6. Digging/rehandles worden gemodelleerd (anders klopt de ASC-productiviteit nooit).
7. Twistlocks als vaste 12 s; lashing platform en dual-trolley later.
8. Konecranes-ordegrootte als defaults, allemaal overschrijfbaar per equipment in de layout (`kinematics:`), zodat een ander merk later alleen andere getallen is.
