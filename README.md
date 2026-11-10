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
| `agv_wait_tp_s_mean` | alleen LOAD: gemiddelde wachttijd van een AGV aan het TP, van aankomst tot de box op de AGV staat (`tp_to_agv`) (s); in Plan A 0, want de AGV wordt pas gestuurd als de box op het TP staat |
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
- **Tests**: `uv run pytest -m e2e_opcua` start sim en gateway als subprocessen op vrije poorten en draait drie tests: 20 containers / 1 STS / 2 AGV's met 10 LOAD-orders (≈ 45 s), hetzelfde kleine scenario (alleen lossen) bij ×25 vergeleken met in-process (≈ 2 min) en de baseline bij ×100 vergeleken met in-process (≈ 3,5 min). Ze staan standaard uit (`addopts = "-m 'not e2e_opcua'"` in `pyproject.toml`) omdat de suite al ≈ 3 minuten duurt.
- **Afwijkingen van het ontwerp**: (1) KPI-tijden lopen vanaf de start van de run (de sim-tijd `t0` bij `Start`, `finish(now - t0)`), niet vanaf sim-tijd 0, zodat een sim met `--autostart` geen voorlooptijd meetelt. (2) De runner roept `kpi.finish()` aan vóór `Stop`, omdat de gateway gestopte servers als `Fault` meldt en die anders als storingen zouden meetellen. (3) Events worden elke 5 ms wandklok afgeleverd (`drain_events`) in plaats van één keer per sample-venster van 60 s, omdat de ECS vanuit zijn event-callback dispatcht en anders tot 60 s sim per overdracht zou achterlopen.
- **Pacing-latentie en toleranties**: tellingen (`containers_done/failed`, `replans`, `faults`) zijn via OPC UA exact gelijk aan in-process, maar de tijd-KPI's lopen structureel iets achter. Elke overdracht event → gateway → runner → `AssignJob` kost 50–100 ms wandklok; bij `speed_factor` 100 is dat 5–10 s sim per overdracht (plus afronding op de sim-tick van 1 s), en omdat de STS de bottleneck is telt dat over de hele run op. `test_baseline_opcua_matches_inprocess` (×100) staat daarom `moves_per_hour_total` binnen 10 % en `vessel_turnaround_s` binnen 900 s toe; `test_small_scenario_gap_shrinks_with_lower_speed` laat zien dat de afwijking bij ×25 krimpt (binnen 3 % en 120 s). Een step-on-demand-klok (de sim wacht op de runner in plaats van op de wandklok) zou de afwijking wegnemen; dat is een optie voor B2/B3.

**Architectuur.** `terminal_sim.serve` draait de salabim-wereld en publiceert elk apparaat als eigen OPC UA-server, plus een klokserver met `SimTime`, `SpeedFactor`, `Start`, `Stop` en `PrefillYard`. `terminal_gateway` spiegelt al die servers onder één namespace (`Terminal/Quay|Fleet|Yard|Clock`), stuurt methodes door en meldt een weggevallen server als `Fault`. De runner praat alleen met de gateway via `OpcUaEquipmentPort`, dat het `EquipmentPort`-protocol implementeert; events worden in de runner-thread afgeleverd (`drain_events`, elke 5 ms wandklok). De ECS (`terminal_ecs`) is daarvoor niet gewijzigd: `test_ecs_unchanged_since_plan_a` vergelijkt een sha256 over de ECS-bronnen met de waarde van `master` in `tests/fixtures/ecs_sources.sha256`.

## Verder

Ontwerp: `docs/superpowers/specs/2026-11-10-terminal-platform-fase1-design.md` en `docs/superpowers/specs/2026-11-10-plan-b1-opcua-design.md`. Plan B2 (ECS-service, viewer, Docker) en B3 (eigen DataTypes, beveiliging, vendor-adapters) volgen.
