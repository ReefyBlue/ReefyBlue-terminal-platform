# Terminal Platform — Plan B1: OPC UA-laag Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** De Plan A-kern draait als drie processen (sim met OPC UA-equipmentservers, gateway, runner/ECS via `OpcUaEquipmentPort`) met dezelfde KPI's als in-process en een ongewijzigde ECS.

**Architecture:** Nieuw package `terminal_opcua` (namespace, JSON-codecs, client-poort) hangt alleen aan `terminal_model`. `terminal_sim` krijgt per Equipment een asyncua-server en een `serve`-CLI met wandklok-pacing; boxoverdracht gebeurt autonoom in de sim zodat de ECS geen hooks meer nodig heeft. Nieuw package `terminal_gateway` spiegelt alle servers onder één namespace. De runner krijgt `--port opcua`.

**Tech Stack:** Python 3.12+, asyncua 2.1, salabim, uv workspace, pytest (bestaand).

**Spec:** `docs/superpowers/specs/2026-11-10-plan-b1-opcua-design.md` (aanvulling op `2026-11-10-terminal-platform-fase1-design.md`)

## Global Constraints

- `terminal_ecs` wijzigt niet (git diff op `packages/ecs` is leeg aan het einde van dit plan) en importeert alleen `terminal_model`
- Afhankelijkheidsrichting: `opcua` → model; `gateway` → model, opcua; `sim` → model, opcua; `runner` → alles; `tos_stub` → model, ecs (architectuurtest uitbreiden)
- Namespace-URI exact `urn:terminal:model`; boom `Terminal/Clock`, `Terminal/Quay/<STS-id>`, `Terminal/Fleet/<AGV-id>`, `Terminal/Yard/Block-<X>/<ASC-id>`
- Variabelenamen exact: `Id, Kind, Vendor, State, PositionX, PositionY, CurrentJobId, EnergyKwh, Extra, LastEvent, EventSeq`; methoden `AssignJob(jobJson: String) → Boolean`, `CancelJob() → Boolean`, `Reset() → Boolean`; control: `SimTime, SpeedFactor`, methoden `Stop()`, `PrefillYard(json: String) → Boolean`
- JSON: enums als stringwaarde, `None` → `null`, `Position` → `{"x":..,"y":..}`
- Poorten: equipment `base_port + index` (volgorde `World.equipment`), control `base_port + 12`, default `base_port 48400`; gateway 4840; endpoints `opc.tcp://0.0.0.0:<poort>/terminal/<id>` resp. `/terminal/gateway`
- Pacing: sim stapt `SIM_TICK_S = 1.0` sim-seconde per asyncio-stap en slaapt `SIM_TICK_S / speed_factor` wandklok ertussen; variabelen worden na elke stap gesynchroniseerd
- Alle asyncio op één thread in sim en gateway; `OpcUaEquipmentPort` draait zijn eigen loop in een thread en levert events alleen via `drain_events()` af
- Anonymous, geen beveiliging, localhost
- Tests met echte asyncua-servers gebruiken vrije poorten (`socket` bind op 0) en ruimen op in fixtures; totale suite < ~3 min
- Commits: conventional prefix; trailers zoals in Plan A

## Review Focus

1. Twee ECS-events voor hetzelfde apparaat binnen één sim-stap (bijv. `JOB_FAILED` direct gevolgd door `STATE_CHANGED`) → beide moeten bij de client aankomen, in volgorde. Test in Taak 4 (`EventSeq` springt met 2, beide `LastEvent`-waarden ontvangen via per-event buffer).
2. Gateway start vóór de sim → gateway toont alle apparaten als `Fault` en herstelt automatisch zodra de sim er is. Test in Taak 6.
3. `AssignJob` met JSON die wel parseert maar geen geldig `Job` is (ontbrekend veld) → `False`, geen crash van de server. Test in Taak 4.
4. Runner start terwijl de gateway nog niet luistert → 10 pogingen à 2 s, dan exit 1 met duidelijke melding. Test in Taak 8.
5. Sim-proces krijgt SIGTERM midden in een run → sluit servers netjes, exit 0, geen traceback. Test in Taak 5.

---

## Bestandsstructuur

```
packages/opcua/pyproject.toml                        terminal-opcua: model, asyncua>=2.1
packages/opcua/src/terminal_opcua/
  __init__.py
  codec.py          job/event ↔ JSON
  nodeset.py        namespace-URI, type-definities, object-aanmaak helpers
  port.py           OpcUaEquipmentPort
packages/gateway/pyproject.toml                      terminal-gateway: model, opcua, pyyaml
packages/gateway/src/terminal_gateway/
  __init__.py
  mirror.py         EquipmentMirror (één client → één gateway-object)
  gateway.py        Gateway (config → mirrors, control-doorgifte)
  serve.py          CLI
packages/sim/src/terminal_sim/
  opcua_servers.py  EquipmentServer, ControlServer
  serve.py          CLI met pacing
  equipment/*.py    autonome overdracht (Taak 3)
  world.py          handover-registratie (Taak 3)
packages/runner/src/terminal_runner/run.py           --port opcua
tests/opcua/  tests/gateway/  tests/sim/test_serve.py  tests/e2e/test_opcua_*.py
```

---

### Task 1: Package `terminal_opcua` + codecs

**Files:**
- Create: `packages/opcua/pyproject.toml`, `packages/opcua/src/terminal_opcua/{__init__,codec}.py`
- Modify: root `pyproject.toml` (pythonpath, dev-group), `uv.lock`
- Test: `tests/opcua/test_codec.py`

**Interfaces:**
- Produces (`codec.py`): `job_to_json(job: Job) -> str`, `job_from_json(s: str) -> Job` (raise `ValueError` bij ontbrekend veld of ongeldige enum), `event_to_json(ev: EquipmentEvent) -> str`, `event_from_json(s: str) -> EquipmentEvent`, `snapshot_extra_to_json(extra: Mapping) -> str`, `extra_from_json(s: str) -> dict`

- [ ] **Stap 1: Tests** — `test_job_roundtrip` (alle velden incl. `deadline=None` en `status=RUNNING`), `test_event_roundtrip` (met `new_state=State.FAULT`, `reason=None`), `test_job_from_json_missing_field_raises` (`ValueError`), `test_job_from_json_bad_enum_raises`, `test_extra_roundtrip_nested` (`{"route": [[1,2],[3,4]], "battery_pct": 97.5}`).
- [ ] **Stap 2: Run** `uv run pytest tests/opcua -q` → FAIL (ImportError)
- [ ] **Stap 3: Implementeer** package (hatchling, deps `terminal-model`, `asyncua>=2.1`), `codec.py` met `json` + `dataclasses.asdict`; voeg `packages/opcua/src` toe aan pytest `pythonpath` en `terminal-opcua` aan de root dev-group; `uv lock && uv sync`.
- [ ] **Stap 4: Run** → PASS; `uv run python -c "import asyncua; print(asyncua.__version__)"` toont 2.1.x
- [ ] **Stap 5: Commit** `feat(opcua): package scaffold and JSON codecs`

---

### Task 2: Namespace-definitie

**Files:**
- Create: `packages/opcua/src/terminal_opcua/nodeset.py`
- Test: `tests/opcua/test_nodeset.py`

**Interfaces:**
- Produces:
  - `NAMESPACE_URI = "urn:terminal:model"`
  - `EQUIPMENT_VARS = ("Id","Kind","Vendor","State","PositionX","PositionY","CurrentJobId","EnergyKwh","Extra","LastEvent","EventSeq")`
  - `KIND_VARS: dict[Kind, tuple[str, ...]]` — STS: `BerthSide, SpreaderHeightM, TrolleyPositionM, HandoverLaneOccupied`; AGV: `BatteryPct, LoadedContainerId, Route, SpeedMps`; ASC: `BlockId, GantryPositionM, WsTpOccupied, LsTpOccupied`
  - `EXTRA_KEY_MAP: dict[str, str]` — `extra`-sleutel → variabelenaam (bijv. `"battery_pct" → "BatteryPct"`, `"ws_tp_occupied" → "WsTpOccupied"`); onbekende sleutels blijven alleen in `Extra`
  - `@dataclass class Types: equipment: NodeId; sts: NodeId; agv: NodeId; asc: NodeId; control: NodeId`
  - `async def register_types(server: asyncua.Server) -> tuple[int, Types]` — registreert de namespace en maakt de ObjectTypes met variabelen (met `writable` voor serverzijde) en methoden-declaraties
  - `@dataclass class EquipmentHandlers: assign_job: Callable[[str], bool]; cancel_job: Callable[[], bool]; reset: Callable[[], bool]`
  - `async def create_equipment_object(server, idx: int, types: Types, parent: Node, snap: EquipmentSnapshot, handlers: EquipmentHandlers) -> Node` — instantieert het juiste subtype onder `parent` met browse-naam `snap.equipment_id`, koppelt methoden, vult variabelen uit de snapshot
  - `async def create_control_object(server, idx, types, parent, *, stop: Callable[[], None], prefill: Callable[[str], bool]) -> Node` — browse-naam `Clock`
  - `async def ensure_folders(server, idx, root: Node) -> dict[str, Node]` — maakt `Terminal`, `Terminal/Quay`, `Terminal/Fleet`, `Terminal/Yard` en geeft ze terug; `block_folder(server, idx, yard: Node, block: str) -> Node` maakt/geeft `Block-<X>`
  - `async def write_snapshot(node: Node, snap: EquipmentSnapshot) -> None` — schrijft alle `EQUIPMENT_VARS` behalve `LastEvent/EventSeq` plus de kind-variabelen uit `extra` via `EXTRA_KEY_MAP`
  - `async def emit_event(node: Node, ev: EquipmentEvent) -> None` — schrijft `LastEvent` en verhoogt `EventSeq` atomair in die volgorde (eerst `LastEvent`, dan `EventSeq`)

- [ ] **Stap 1: Tests** (fixture `server` = `asyncua.Server()` op vrije poort, `async with`): `test_types_registered` (idx > 1; `Types`-nodes bestaan; `EquipmentType` heeft alle `EQUIPMENT_VARS`; `AGVType` heeft `BatteryPct`), `test_create_agv_object_and_read` (snapshot met `extra={"battery_pct": 80.0, "route": [[1,2]]}` → `BatteryPct == 80.0`, `Route == "[[1, 2]]"`, `Extra` JSON parseert terug), `test_method_calls_handlers` (client `call_method("AssignJob", "{}")` → handler aangeroepen met `"{}"`, returnt `False` als handler False geeft), `test_emit_event_increments_seq` (`EventSeq` 0 → 1, `LastEvent` parseert naar het event).
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer** `nodeset.py` (asyncua `add_object_type`, `add_variable`, `add_method` met `uamethod`).
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(opcua): namespace types, object creation and event emission`

---

### Task 3: Autonome boxoverdracht in de sim; hooks uit de runner

**Files:**
- Modify: `packages/sim/src/terminal_sim/equipment/{sts,agv,asc}.py`, `packages/sim/src/terminal_sim/world.py`
- Modify: `packages/runner/src/terminal_runner/run.py` (verwijder `_transfer_hook`, `on_transfer`), `packages/runner/src/terminal_runner/kpi.py` (`observe_transfer` weg; TP-wacht uit `job_log`)
- Test: `tests/sim/test_handover.py`, `tests/runner/test_kpi.py` (aanpassen), `tests/e2e/test_plan_a_equivalence.py`

**Interfaces:**
- Produces (`world.py`): `World.handover: Handover` met
  - `class Handover: __init__(self, world)`; `on_event(ev: EquipmentEvent) -> None` — geabonneerd als eerste subscriber op `SimEquipmentPort` (vóór de ECS) zodat de box verplaatst is voordat de ECS het event ziet
  - Regels exact spec §3; `STS` `DISCHARGE` zonder passende AGV → `sts.fail_job("NO_AGV_IN_LANE")` **in plaats van** `complete_job` (dus de STS-klasse roept vóór voltooiing `world.handover.try_discharge(sts) -> bool` aan — maak dat een injecteerbare callback `on_discharge_done: Callable[[STS], bool]` op STS, default `lambda s: True`)
  - AGV: `on_arrival: Callable[[AGV], None]` callback, default no-op, door World gezet: bij aankomst op een TP-cel met box → `unload()` en ASC `tp_ws_occupied = True`, `tp_box[block] = cid`; zonder box en `tp_box[block] == agv.job_container_id` → `load`, `tp_ws_occupied = False`
  - ASC: na `STACK_OUT` voltooid `world.handover.tp_box[block] = cid`; na `STACK_IN` voltooid `tp_box.pop(block)`; via bestaande events (Handover.on_event)
  - STS `LOAD` voltooid → AGV in baan met `loaded_container_id == cid` → `unload()`
- Produces (`kpi.py`): `agv_wait_tp_s_mean` = voor LOAD-orders: tijd tussen `JOB_COMPLETED` van de TRANSPORT naar `TP/..` en de toewijzing (uit `dispatcher.job_log`) van de volgende TRANSPORT van dezelfde AGV; `observe_transfer` verwijderd
- Consumes: `Dispatcher.on_transfer` blijft `None` (ECS ongewijzigd)

- [ ] **Stap 0: Referentie vastleggen** — vóór enige wijziging: `uv run python -m terminal_runner.run scenarios/baseline.yaml --replications 1 --out /tmp/ref` en kopieer de KPI-rij naar `tests/e2e/fixtures/plan_a_baseline_seed1.json` (alle sleutels). Commit apart: `test: capture Plan A baseline KPIs as reference`.
- [ ] **Stap 1: Tests** — `tests/sim/test_handover.py`: `test_discharge_puts_box_on_sent_agv` (World, AGV-01 TRANSPORT naar STS-01 met `container_id="C1"`, STS DISCHARGE C1 → na run `AGV-01.loaded_container_id == "C1"`), `test_discharge_without_matching_agv_fails_job` (`JOB_FAILED reason NO_AGV_IN_LANE`), `test_agv_unloads_at_tp_and_asc_flag_set`, `test_stack_out_then_agv_loads_at_tp`, `test_load_empties_agv`. `tests/e2e/test_plan_a_equivalence.py`: `run_once(baseline, layout, 1)` == fixture-JSON voor alle sleutels behalve `agv_wait_tp_s_mean` (nieuwe definitie; assert `>= 0`).
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer** callbacks + `Handover`; verwijder hooks uit `run.py`; pas `kpi.py` en `tests/runner/test_kpi.py` aan (`on_transfer`-gebruik vervangen door `job_log`-gebaseerde paring).
- [ ] **Stap 4: Run** `uv run pytest -q` → alles PASS, equivalentietest exact gelijk. Als tellingen afwijken: oorzaak zoeken (volgorde van subscribers), niet de fixture aanpassen.
- [ ] **Stap 5: Commit** `feat(sim): autonomous box handover; runner drops transfer hooks`

---

### Task 4: Equipment-OPC UA-server

**Files:**
- Create: `packages/sim/src/terminal_sim/opcua_servers.py`
- Modify: `packages/sim/pyproject.toml` (dep `terminal-opcua`), `uv.lock`
- Test: `tests/sim/test_opcua_servers.py`

**Interfaces:**
- Produces: `class EquipmentServer`:
  - `__init__(self, equipment: Equipment, *, port: int, host: str = "0.0.0.0")`
  - `async def start(self) -> None` — maakt `asyncua.Server`, endpoint `opc.tcp://{host}:{port}/terminal/{equipment_id}`, registreert typen, maakt `Terminal/<Quay|Fleet|Yard/Block-X>/<id>` object; handlers roepen `equipment.assign_job(job_from_json(s))` (ValueError → `False`), `cancel_job`, `reset`
  - `async def stop(self) -> None`
  - `def on_emit(self, ev: EquipmentEvent) -> None` — synchroon vanuit de simloop: zet `(ev)` op een interne lijst
  - `async def sync(self) -> None` — schrijft `write_snapshot(equipment.snapshot())` en daarna voor elk gebufferd event `emit_event` in volgorde (zo komen meerdere events per stap allemaal aan)
  - `endpoint: str`
- Consumes: Taak 2

- [ ] **Stap 1: Tests** (fixture: `Environment(yieldless=False)`, `Grid`, één `STS` met `emit=server.on_emit`, vrije poort): `test_assign_via_opcua_runs_job` (client `AssignJob(job_to_json(DISCHARGE))` → True; reserveer lane als AGV; `env.run(300)`; `await server.sync()`; `EventSeq >= 2`, `State == "Idle"`, `EnergyKwh == 5.0`), `test_two_events_one_step_both_delivered` (cancel_job + reset → sync → client ziet `EventSeq` +2 en bij abonnement op `EventSeq` twee datachanges; bij elke datachange `LastEvent` lezen geeft achtereenvolgens JOB_FAILED en STATE_CHANGED — implementeer de buffer zo dat `emit_event` per event een aparte write doet), `test_invalid_json_returns_false` (`AssignJob("{\"job_id\": 1}")` → False, server leeft nog), `test_wrong_state_returns_false`.
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): OPC UA server per equipment`

---

### Task 5: Control-server en `serve`-CLI met pacing

**Files:**
- Create: `packages/sim/src/terminal_sim/serve.py`; uitbreiden `opcua_servers.py` met `ControlServer`
- Test: `tests/sim/test_serve.py`

**Interfaces:**
- Produces (`opcua_servers.py`): `class ControlServer`: `__init__(self, world: World, *, port: int, on_stop: Callable[[], None])`; `start/stop`; `async def sync(self)` schrijft `SimTime = world.now()`, `SpeedFactor`; methode `Stop()` → `on_stop()`; `PrefillYard(json)` → parse lijst `[{"slot","container_id"}]`, `world.yard.occupy(parse_location(slot), cid)` voor elk; `False` bij fout of als `world.now() > 0`
- Produces (`serve.py`):
  - `SIM_TICK_S = 1.0`
  - `class SimService`: `__init__(self, scenario: Scenario, layout: Layout, *, base_port: int = 48400, host="0.0.0.0")`; `async def run(self) -> None` — start 12 `EquipmentServer`s + `ControlServer`, dan loop: `world.run(until=world.now()+SIM_TICK_S)`; `await sync()` op alle servers; `await asyncio.sleep(SIM_TICK_S / scenario.speed_factor)`; WARNING als wandklokachterstand > 5 s; stopt op `stop()` of SIGTERM; sluit servers; `def stop(self)`
  - `main(argv) -> int`: `scenario` (pad), `--layout` (default `terminal.yaml`), `--base-port` (default 48400), `--host`; JSON-logging; SIGTERM/SIGINT → nette stop, exit 0
- Consumes: Taak 4

- [ ] **Stap 1: Tests** — `test_sim_time_advances_with_pacing` (SimService met `speed_factor 200`, tmp-scenario 1 STS/2 AGV/4 containers, vrije base_port; start als asyncio-task; na 0,5 s wandklok `SimTime` tussen 60 en 140; `stop()`), `test_prefill_yard_occupies` (client `PrefillYard('[{"slot":"A/1/1/1","container_id":"L1"}]')` → True en `world.yard.locate("L1") == A/1/1/1`; tweede keer na eerste run-stap → False), `test_sigterm_exits_cleanly` (subprocess `python -m terminal_sim.serve tmp.yaml --base-port N`, wacht tot control-endpoint bereikbaar, `SIGTERM`, `returncode == 0`, stderr zonder "Traceback").
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): serve CLI with wall-clock pacing and control server`

---

### Task 6: Gateway

**Files:**
- Create: `packages/gateway/pyproject.toml`, `packages/gateway/src/terminal_gateway/{__init__,mirror,gateway,serve}.py`
- Modify: root `pyproject.toml`, `uv.lock`, `terminal.yaml` (per STS/blok/AGV geen endpoints: afleiden uit `base_port` — voeg top-level `opcua: {base_port: 48400, host: localhost}` toe)
- Test: `tests/gateway/test_gateway.py`

**Interfaces:**
- Produces (`mirror.py`): `class EquipmentMirror`: `__init__(self, server: asyncua.Server, idx, types, parent: Node, *, equipment_id: str, kind: Kind, endpoint: str)`; `async def start(self)` — maakt gateway-object (initieel `State="Fault"`, `Vendor=""`), connect-lus: client → subscribe op alle variabelen (interval 50 ms) → bij datachange schrijven naar eigen variabele; `LastEvent/EventSeq` doorgeven in volgorde (eerst LastEvent, dan EventSeq); methoden `AssignJob/CancelJob/Reset` doorsturen met time-out 2 s (fout → `False`); verbinding weg → `State="Fault"`, `LastEvent` = STATE_CHANGED Fault met `sim_time` = laatst bekende `SimTime` (gateway geeft die door via `set_sim_time(t)`), `EventSeq += 1`; reconnect elke 5 s; na herstel alle variabelen opnieuw lezen; `async def stop(self)`
- Produces (`gateway.py`): `class Gateway`: `__init__(self, layout: Layout, *, port: int = 4840, upstream_host: str, base_port: int)`; `async def start/stop`; maakt `Terminal/*`-mappen, 12 mirrors en `Terminal/Clock` dat `SimTime/SpeedFactor` spiegelt en `Stop/PrefillYard` doorstuurt; `equipment_ids()`
- Produces (`serve.py`): `main(argv)`: `--config terminal.yaml`, `--port 4840`, `--upstream-host` (default uit yaml), `--base-port`; SIGTERM netjes
- Consumes: Taak 2, 4, 5

- [ ] **Stap 1: Tests** — `test_mirror_reflects_variable_change` (één `EquipmentServer` + Gateway op vrije poorten; `equipment.set_state(BUSY)`, `sync()` → binnen 200 ms leest client op gateway `State == "Busy"`), `test_method_forwarded` (`Reset` via gateway → equipment IDLE), `test_gateway_before_sim_recovers` (Gateway starten zonder server → `State == "Fault"`; dan server starten → binnen 6 s `State == "Idle"`; abonnee op `EventSeq` zag het Fault-event), `test_server_drop_marks_fault` (server `stop()` → gateway `State == "Fault"` binnen 3 s), `test_prefill_forwarded`.
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**; architectuurtest uitbreiden: `gateway` importeert alleen `terminal_model`, `terminal_opcua`.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(gateway): OPC UA aggregation gateway with reconnect`

---

### Task 7: `OpcUaEquipmentPort`

**Files:**
- Create: `packages/opcua/src/terminal_opcua/port.py`
- Test: `tests/opcua/test_port.py`

**Interfaces:**
- Produces: `class OpcUaEquipmentPort` (implementeert `EquipmentPort`):
  - `__init__(self, endpoint: str, *, connect_timeout_s: float = 2.0)`; `connect() -> None` (start thread + loop, browse, subscribe op alle `EventSeq`-nodes en `SimTime`); `close()`; `__enter__/__exit__`
  - `equipment_ids() -> list[str]` (STS, AGV, ASC in id-volgorde)
  - `snapshot(id) -> EquipmentSnapshot` (`read_values` van alle `EQUIPMENT_VARS`; `extra` uit `Extra`; `KeyError` onbekend)
  - `assign_job(id, job) -> bool`, `cancel_job(id) -> bool`, `reset(id) -> bool` (methode-aanroep; time-out → `False` + WARNING)
  - `subscribe(cb)`; `drain_events() -> int` — levert gebufferde events in ontvangstvolgorde aan alle callbacks, geeft aantal; gaten in `EventSeq` → WARNING
  - `now() -> float` (laatst ontvangen `SimTime`; na `connect` eerst gelezen)
  - `prefill_yard(slots: list[tuple[str, str]]) -> bool` (buiten protocol)
  - `wait_for_sim_time(t: float, timeout_s: float) -> bool` (blokkeert tot `SimTime >= t`; voor de runner-lus)
- Consumes: Taak 2, 6

- [ ] **Stap 1: Tests** (fixture: World 1 STS/2 AGV/1 ASC in-process gedraaid via een `SimService` in een thread? Nee: gebruik `EquipmentServer`s + `ControlServer` + `Gateway` in de test-eventloop en de poort in zijn eigen thread): `test_port_satisfies_protocol`, `test_equipment_ids_order`, `test_snapshot_matches_sim` (positie, state, `extra["battery_pct"]`), `test_assign_and_drain_events` (assign TRANSPORT AGV-01 → sim `run(200)` + `sync()` → `drain_events() >= 2`, laatste event JOB_COMPLETED), `test_now_follows_sim_time`, `test_unknown_id_keyerror`, `test_events_seq_gap_logged` (schrijf `EventSeq` kunstmatig +2 op de server → WARNING in caplog).
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**.
- [ ] **Stap 4: Run** → PASS; bestaande `tests/ecs` ongewijzigd groen
- [ ] **Stap 5: Commit** `feat(opcua): OpcUaEquipmentPort client implementation`

---

### Task 8: Runner `--port opcua`, e2e, architectuur, README

**Files:**
- Modify: `packages/runner/src/terminal_runner/run.py`, `packages/runner/pyproject.toml` (deps opcua), `README.md`, `tests/test_architecture.py`
- Test: `tests/runner/test_run_opcua.py`, `tests/e2e/test_opcua_small.py`, `tests/e2e/test_opcua_equivalence.py`

**Interfaces:**
- Produces (`run.py`): `run_once_opcua(scenario, layout, seed, *, gateway: str) -> dict[str, float]` — connect met 10 pogingen à 2 s (dan `SystemExit(1)` met melding); `prefill_yard`; `submit`; loop: `port.wait_for_sim_time(t, timeout)`; `drain_events()`; `tick()`; `sample()`; stop op `open == 0` → `Stop()` via control; CLI `--port {inprocess,opcua}` (default inprocess), `--gateway opc.tcp://localhost:4840`
- Architectuurtest: `opcua` → alleen `terminal_model`; `gateway` → model + opcua; `sim` → model + opcua; `ecs` ongewijzigd (`git diff --stat master -- packages/ecs` leeg; test leest `git` niet maar vergelijkt een vaste hash van de ecs-bronbestanden met een fixture-waarde vastgelegd in Stap 0)

- [ ] **Stap 0: ECS-hash vastleggen** — `tests/fixtures/ecs_sources.sha256` = sha256 over gesorteerde inhoud van `packages/ecs/src/**/*.py` op `master`; test `test_ecs_unchanged_since_plan_a`.
- [ ] **Stap 1: Tests** — `tests/runner/test_run_opcua.py::test_gateway_unreachable_exits_1` (monkeypatch retry-sleep naar 0; `SystemExit` met code 1, melding bevat endpoint); `tests/e2e/test_opcua_small.py::test_small_scenario_completes_over_opcua` (subprocessen `terminal_sim.serve` en `terminal_gateway.serve` op vrije poorten; scenario 20 containers / 1 STS / 2 AGV / `speed_factor 200`; `run_once_opcua` → `containers_done == 20`, `timed_out == 0`, wandklok < 60 s; processen netjes gestopt); `tests/e2e/test_opcua_equivalence.py::test_baseline_opcua_matches_inprocess` (`@pytest.mark.e2e_opcua`; baseline seed 1 `speed_factor 200` via OPC UA vs `run_once` in-process: `containers_done/failed/faults/replans` gelijk, `moves_per_hour_total` binnen 2 %, `vessel_turnaround_s` binnen 60 s; log beide dicts bij afwijking).
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**; README: nieuwe sectie "OPC UA-modus" met de drie CLI's, poorten, `speed_factor`-betekenis, en de aantekening dat `e2e_opcua` met `-m e2e_opcua` apart kan draaien als de suite te lang wordt.
- [ ] **Stap 4: Run** `uv run pytest -q` → PASS; noteer wandkloktijd; als > 3 min: registreer `e2e_opcua` als deselect-by-default via `addopts = "-m 'not e2e_opcua'"` en documenteer.
- [ ] **Stap 5: Commit** `feat(runner): OPC UA port mode, e2e tests, docs`

---

## Zelfreview

- **Spec-dekking:** §2 processen/klok → T5, T8; §3 overdracht → T3; §4 model/codecs → T1, T2; §5 servers → T4, T5; §6 gateway → T6; §7 poort → T7; §8 runner → T8; §9 pakketten → T1, T6, T8 (architectuurtest); §10 fouten → T4 (ongeldige JSON), T6 (reconnect, Fault, time-out), T7 (gap), T8 (gateway onbereikbaar), T5 (achterstand-WARNING, SIGTERM); §11 testen → per taak + T3 equivalentie + T8 e2e.
- **Typeconsistentie:** `EquipmentHandlers`, `write_snapshot`, `emit_event` (T2) gebruikt in T4/T6; `drain_events`, `wait_for_sim_time`, `prefill_yard` (T7) gebruikt in T8; `SIM_TICK_S` (T5) genoemd in constraints.
- **Review Focus:** 1 → T4 `test_two_events_one_step_both_delivered`; 2 → T6 `test_gateway_before_sim_recovers`; 3 → T4 `test_invalid_json_returns_false`; 4 → T8 `test_gateway_unreachable_exits_1`; 5 → T5 `test_sigterm_exits_cleanly`.
- **Proportie:** 8 taken voor een spec van 12 secties; code-blokken beperkt tot signatures en testnamen.
