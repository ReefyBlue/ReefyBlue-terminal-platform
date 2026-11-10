# Terminal Platform — fase 1 kern

Fase 1 van het terminalplatform voor een volautomatische containerterminal: een discrete-event simulator (salabim) van een fictieve terminal, de ECS-dispatchlogica erachter via de `EquipmentPort`, een TOS-stub en een scenario-runner die KPI's oplevert. Alles draait in-process; de OPC UA-laag volgt in Plan B.

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

Tests: `uv run pytest -q` (inclusief de e2e-runs met 200 containers, marker `e2e`; uitsluiten met `-m "not e2e"`).

## Scenario-yaml (`scenarios/*.yaml`)

| Veld | Betekenis |
|---|---|
| `name` | naam; bepaalt de bestandsnamen van de uitvoer |
| `seed` | startseed (replicatie i gebruikt `seed + i`) |
| `n_sts` | aantal STS-kranen (1..2) |
| `n_agv` | aantal AGV's (1..6) |
| `n_containers` | aantal containers (werkorders) |
| `share_40ft` | aandeel 40ft-containers (0..1) |
| `discharge_share` | aandeel lossen (1.0 = alleen lossen, 0 = alleen laden) |
| `speed_factor` | snelheidsfactor van de gesimuleerde apparatuur |
| `duration_s` | harde bovengrens op de gesimuleerde tijd (s) |
| `faults` | optioneel; per `sts`/`agv`/`asc`: `mtbf_s`, `mttr_s` (mtbf 0 = geen storingen) |

## KPI-kolommen

| Kolom | Betekenis |
|---|---|
| `moves_per_hour_total` | moves per gesimuleerd uur over alle STS'en |
| `moves_per_hour_<STS-id>` | idem per STS-kraan (bijv. `moves_per_hour_STS-01`) |
| `agv_wait_s_mean` | gemiddelde stilstandtijd van een bezette AGV per transport (s) |
| `agv_travel_s_mean` | gemiddelde rijtijd van een bezette AGV per transport (s) |
| `utilisation_STS` / `_AGV` / `_ASC` | aandeel samples waarin het apparaattype bezig was |
| `vessel_turnaround_s` | tijd van de eerste tot de laatste voltooide STS-move (s) |
| `faults` | aantal opgetreden storingen |
| `replans` | aantal herplanningen door de dispatcher |
| `energy_kwh_per_move` | energieverbruik per STS-move (kWh) |
| `containers_done` | aantal afgeronde containers |
| `containers_failed` | aantal mislukte containers |
| `sim_elapsed_s` | werkelijk gesimuleerde tijd van de run (s) |
| `timed_out` | 1.0 als `duration_s` bereikt werd voordat alle orders klaar waren, anders 0.0 |

## Architectuur

Workspace-packages in `packages/`:

- `model` (`terminal_model`): gedeelde types, locaties, JSON-logging en de `EquipmentPort`-interface.
- `sim` (`terminal_sim`): layout, scenario, `World` met STS, AGV, ASC, stack en storingen.
- `ecs` (`terminal_ecs`): `Dispatcher` en werkorders; praat alleen met `EquipmentPort`.
- `tos_stub` (`terminal_tos_stub`): slottoewijzing en werkordergenerator.
- `runner` (`terminal_runner`): koppelt sim, ecs en tos_stub; KPI-collector en CLI.

Importregel (bewaakt door `tests/test_architecture.py`): `model` importeert geen ander `terminal_*`-package; `sim` importeert nooit `ecs`, `tos_stub` of `runner`; `ecs` importeert nooit `sim`. Alleen `runner` kent alle packages.

## Verder

Ontwerp: `docs/superpowers/specs/2026-11-10-terminal-platform-fase1-design.md`. Plan B (OPC UA-laag, gateway, viewer, Docker) volgt.
