# Terminal Platform — Plan A: Kern (model, simulatie, ECS, runner)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Een in-process werkende terminal-simulatie (2 STS, 6 AGV, 4 ASC) met ECS-dispatch achter een poortinterface en een scenario-runner die reproduceerbare KPI-rapporten levert.

**Architecture:** `terminal_model` definieert typen, locaties en de `EquipmentPort`-interface. `terminal_sim` bouwt de Salabim-wereld en implementeert die poort. `terminal_ecs` bevat de dispatch en kent alleen de poort (nooit de sim). `terminal_tos_stub` genereert werkorders. De runner koppelt alles in-process en schrijft KPI's. Plan B vervangt later de in-process poort door OPC UA-servers + gateway zonder de ECS te wijzigen.

**Tech Stack:** Python 3.12, uv (workspace), salabim ≥ 25, pyyaml, pytest. Nog geen asyncua, FastAPI of Docker (Plan B).

**Spec:** `docs/superpowers/specs/2026-11-10-terminal-platform-fase1-design.md`

## Global Constraints

- Python 3.12; afhankelijkheden per package in `pyproject.toml`; `uv` workspace in de root
- `terminal_ecs` importeert nooit uit `terminal_sim` (afgedwongen door test in Taak 16)
- Alle tijd in seconden sim-tijd (float); afstanden in meters; energie in kWh
- Alle willekeur via één `random.Random(seed)` per run; geen `random.random()` zonder seed
- Locatie-notatie exact: `STS-01`, `AGV-03`, `ASC-A`, `A/12/3/2` (blok/bay/row/tier, 1-based), `TP/A/WS`
- Equipment-ID's: `STS-01`, `STS-02`, `AGV-01`…`AGV-06`, `ASC-A`…`ASC-D`
- Logregels als JSON naar stdout via `logging` met één JSON-formatter in `terminal_model.logging`
- Commits in het Nederlands of Engels, conventional-commits-prefix (`feat:`, `test:`, `chore:`)

## Review Focus

1. Werkorder voor een container die al in de stack staat (dubbele `container_id`) → ECS weigert met `DUPLICATE_CONTAINER`, geen crash. Test in Taak 11.
2. Scenario met 0 AGV's of 0 STS → runner stopt met duidelijke fout vóór de run begint, niet met een deadlock. Test in Taak 3.
3. Blok vol (alle 40×6×4 slots bezet) → `find_slot` geeft `None`, ECS vraagt `SlotProvider` om ander blok; als geen blok vrij → job `Failed(YARD_FULL)`, run eindigt netjes. Test in Taak 4 en 12.
4. Twee AGV's willen tegelijk dezelfde gridcel → precies één krijgt reservering; de ander wacht; geen dubbele bezetting. Test in Taak 5.
5. `CancelJob` op een AGV midden in een rit → AGV blijft op laatst gereserveerde cel staan, cel blijft gereserveerd tot `Reset`, geen container "verdwijnt". Test in Taak 7.

---

## Bestandsstructuur

```
terminal-platform/
  pyproject.toml                       uv workspace, pytest-config
  terminal.yaml                        layout + equipment (spec §4)
  scenarios/baseline.yaml              seed 1, 6 AGV, 2 STS, 200 containers, geen storingen
  scenarios/agv4.yaml                  als baseline, 4 AGV
  scenarios/faults.yaml                als baseline, MTBF/MTTR aan
  packages/model/src/terminal_model/
    __init__.py
    types.py          enums + Position, Job, Container
    locations.py      parse/format van locatiestrings
    port.py           EquipmentPort-protocol, EquipmentSnapshot, EquipmentEvent
    logging.py        JSON-formatter
  packages/sim/src/terminal_sim/
    __init__.py
    scenario.py       Scenario (yaml → dataclass + validatie)
    layout.py         Layout (terminal.yaml → dataclass)
    yard.py           Yard: slotkeuze, bezetting
    grid.py           Grid: routering + celreserveringen
    equipment/base.py Equipment(sim.Component)
    equipment/sts.py  STS
    equipment/agv.py  AGV
    equipment/asc.py  ASC
    faults.py         FaultInjector
    world.py          World + SimEquipmentPort
    kpi.py            KpiCollector
    run.py            CLI
  packages/ecs/src/terminal_ecs/
    __init__.py
    workorders.py     WorkOrder, SlotProvider-protocol
    dispatcher.py     Dispatcher
  packages/tos_stub/src/terminal_tos_stub/
    __init__.py
    generator.py      werkorders uit Scenario
    allocator.py      SimpleSlotAllocator (implementeert SlotProvider)
  tests/
    model/ sim/ ecs/ tos_stub/ e2e/
    ecs/fake_port.py  FakePort voor contract-tests
```

---

### Task 1: Workspace + model-typen en locaties

**Files:**
- Create: `pyproject.toml`, `packages/model/pyproject.toml`, `packages/sim/pyproject.toml`, `packages/ecs/pyproject.toml`, `packages/tos_stub/pyproject.toml`
- Create: `packages/model/src/terminal_model/{__init__,types,locations,logging}.py`
- Test: `tests/model/test_types.py`, `tests/model/test_locations.py`

**Interfaces:**
- Produces (`types.py`):
  - `class Kind(StrEnum): STS, AGV, ASC`
  - `class State(StrEnum): IDLE, BUSY, FAULT, MAINTENANCE`
  - `class JobType(StrEnum): DISCHARGE, LOAD, STACK_IN, STACK_OUT, TRANSPORT`
  - `class JobStatus(StrEnum): ASSIGNED, RUNNING, DONE, FAILED`
  - `@dataclass(frozen=True) class Position: x: float; y: float` met `distance_to(other) -> float`
  - `@dataclass class Job: job_id: str; type: JobType; container_id: str; from_loc: str; to_loc: str; priority: int = 0; deadline: float | None = None; status: JobStatus = JobStatus.ASSIGNED`
  - `@dataclass(frozen=True) class Container: container_id: str; length_ft: int; weight_t: float; dest_block: str` — `length_ft` ∈ {20, 40}, anders `ValueError`
- Produces (`locations.py`):
  - `@dataclass(frozen=True) class EquipmentLoc: equipment_id: str`
  - `@dataclass(frozen=True) class SlotLoc: block: str; bay: int; row: int; tier: int`
  - `@dataclass(frozen=True) class TransferPointLoc: block: str; side: Literal["WS","LS"]`
  - `Location = EquipmentLoc | SlotLoc | TransferPointLoc`
  - `parse_location(s: str) -> Location` (raise `ValueError` bij ongeldig)
  - `format_location(loc: Location) -> str`
- Produces (`logging.py`): `configure_json_logging(level: int = logging.INFO) -> None`

- [ ] **Stap 1: Workspace aanmaken** — root `pyproject.toml` met `[tool.uv.workspace] members = ["packages/*"]`, `[tool.pytest.ini_options] testpaths = ["tests"]`, pythonpath naar de vier `src`-mappen. Per package een minimale `pyproject.toml` (hatchling, `requires-python = ">=3.12"`). `sim` vereist `salabim>=25`, `pyyaml>=6`, `terminal-model`; `ecs` vereist alleen `terminal-model`; `tos_stub` vereist `terminal-model`, `pyyaml`.
- [ ] **Stap 2: Schrijf falende tests**

```python
# tests/model/test_types.py
def test_container_rejects_invalid_length():
    with pytest.raises(ValueError):
        Container("C1", 45, 20.0, "A")

def test_position_distance():
    assert Position(0, 0).distance_to(Position(3, 4)) == 5.0

# tests/model/test_locations.py
@pytest.mark.parametrize("s,loc", [
    ("STS-01", EquipmentLoc("STS-01")),
    ("A/12/3/2", SlotLoc("A", 12, 3, 2)),
    ("TP/B/WS", TransferPointLoc("B", "WS")),
])
def test_roundtrip(s, loc):
    assert parse_location(s) == loc
    assert format_location(loc) == s

@pytest.mark.parametrize("s", ["", "A/12/3", "TP/A/XX", "A/0/1/1"])
def test_parse_rejects(s):
    with pytest.raises(ValueError):
        parse_location(s)
```

- [ ] **Stap 3: Run** `uv run pytest tests/model -v` → FAIL (ImportError)
- [ ] **Stap 4: Implementeer** `types.py`, `locations.py`, `logging.py` volgens Interfaces. Bay/row/tier zijn ≥ 1.
- [ ] **Stap 5: Run** `uv run pytest tests/model -v` → PASS
- [ ] **Stap 6: Commit** `chore: workspace scaffold + terminal_model types and locations`

---

### Task 2: EquipmentPort-interface

**Files:**
- Create: `packages/model/src/terminal_model/port.py`
- Test: `tests/model/test_port.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class EquipmentSnapshot: equipment_id: str; kind: Kind; vendor: str; state: State; position: Position; current_job_id: str | None; energy_kwh: float; extra: Mapping[str, object]`
  - `class EventKind(StrEnum): STATE_CHANGED, JOB_COMPLETED, JOB_FAILED`
  - `@dataclass(frozen=True) class EquipmentEvent: kind: EventKind; equipment_id: str; sim_time: float; job_id: str | None = None; reason: str | None = None; new_state: State | None = None`
  - `class EquipmentPort(Protocol)`:
    - `def equipment_ids(self) -> list[str]`
    - `def snapshot(self, equipment_id: str) -> EquipmentSnapshot`
    - `def assign_job(self, equipment_id: str, job: Job) -> bool`
    - `def cancel_job(self, equipment_id: str) -> bool`
    - `def reset(self, equipment_id: str) -> bool`
    - `def subscribe(self, callback: Callable[[EquipmentEvent], None]) -> None`
    - `def now(self) -> float`

- [ ] **Stap 1: Test** — `test_port_is_runtime_checkable`: een minimale klasse met alle zeven methodes voldoet aan `isinstance(obj, EquipmentPort)`; een klasse zonder `now` niet.
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer** `port.py` met `@runtime_checkable`.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(model): EquipmentPort protocol, snapshot and event types`

---

### Task 3: Scenario en layout laden

**Files:**
- Create: `terminal.yaml`, `scenarios/baseline.yaml`, `scenarios/agv4.yaml`, `scenarios/faults.yaml`
- Create: `packages/sim/src/terminal_sim/{scenario,layout}.py`
- Test: `tests/sim/test_scenario.py`, `tests/sim/test_layout.py`

**Interfaces:**
- Produces (`scenario.py`):
  - `@dataclass(frozen=True) class FaultParams: mtbf_s: float; mttr_s: float` (0 = geen storingen)
  - `@dataclass(frozen=True) class Scenario: name: str; seed: int; n_sts: int; n_agv: int; n_containers: int; share_40ft: float; discharge_share: float; faults: dict[Kind, FaultParams]; speed_factor: float; duration_s: float`
  - `load_scenario(path: Path) -> Scenario` — raise `ValueError` als `n_sts < 1`, `n_agv < 1`, `n_containers < 1`, `n_sts > 2`, `n_agv > 6`, `not 0 <= share_40ft <= 1`
- Produces (`layout.py`):
  - `@dataclass(frozen=True) class StsSpec: equipment_id: str; position: Position; lane_cells: tuple[tuple[int,int], tuple[int,int]]`
  - `@dataclass(frozen=True) class BlockSpec: block: str; asc_id: str; tp_ws: Position; tp_ws_cell: tuple[int,int]; bays: int; rows: int; tiers: int`
  - `@dataclass(frozen=True) class Layout: quay_length_m: float; grid_cell_m: float; grid_cols: int; grid_rows: int; sts: list[StsSpec]; blocks: list[BlockSpec]; agv_start_cells: list[tuple[int,int]]`
  - `load_layout(path: Path) -> Layout`

**Vaste waarden** (spec §4) in `terminal.yaml`: `quay_length_m: 400`, `grid_cell_m: 20`, grid 20 × 6 cellen (x 0–400, y 0–120), STS-01 op (100, 0) met lane-cellen (4,0),(5,0); STS-02 op (250, 0) met (12,0),(13,0); blokken A–D met `tp_ws` op x = 50, 150, 250, 350 en y = 120, cellen (2,5),(7,5),(12,5),(17,5); 40 bays × 6 rows × 4 tiers; AGV-startcellen (0,2)…(5,2).

- [ ] **Stap 1: Tests**

```python
def test_baseline_loads():
    s = load_scenario(Path("scenarios/baseline.yaml"))
    assert (s.seed, s.n_sts, s.n_agv, s.n_containers) == (1, 2, 6, 200)
    assert s.faults[Kind.AGV].mtbf_s == 0

@pytest.mark.parametrize("field,value", [("n_agv", 0), ("n_sts", 0), ("n_agv", 7), ("share_40ft", 1.5)])
def test_scenario_rejects(tmp_path, field, value): ...  # schrijf yaml met één fout veld → ValueError

def test_layout_geometry():
    L = load_layout(Path("terminal.yaml"))
    assert (L.grid_cols, L.grid_rows) == (20, 6)
    assert L.sts[1].position == Position(250, 0)
    assert L.blocks[1].tp_ws == Position(150, 120)
    assert L.blocks[0].bays * L.blocks[0].rows * L.blocks[0].tiers == 960
```

- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer** beide modules + de vier yaml-bestanden. `faults.yaml`: STS mtbf 7200/mttr 600, AGV mtbf 14400/mttr 300, ASC mtbf 10800/mttr 900. `agv4.yaml`: `n_agv: 4`. Alle scenario's: `share_40ft 0.6`, `discharge_share 1.0`, `speed_factor 50`, `duration_s 43200`.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): scenario and layout loading with validation`

---

### Task 4: Yard

**Files:**
- Create: `packages/sim/src/terminal_sim/yard.py`
- Test: `tests/sim/test_yard.py`

**Interfaces:**
- Produces: `class Yard`:
  - `__init__(self, blocks: list[BlockSpec])`
  - `find_slot(self, block: str) -> SlotLoc | None` — laagste vrije tier in de eerst niet-volle bay; bays oplopend, rows oplopend
  - `is_free(self, slot: SlotLoc) -> bool`
  - `occupy(self, slot: SlotLoc, container_id: str) -> None` — raise `ValueError("SLOT_OCCUPIED")` als bezet
  - `release(self, slot: SlotLoc) -> str` — geeft container_id; raise `KeyError` als leeg
  - `locate(self, container_id: str) -> SlotLoc | None`
  - `occupancy(self, block: str) -> float` (0–1)
  - `capacity(self, block: str) -> int`

- [ ] **Stap 1: Tests** — `test_find_slot_fills_tiers_first` (eerste 4 slots zijn `A/1/1/1..4`, de vijfde `A/1/2/1`), `test_occupy_twice_raises`, `test_block_full_returns_none` (960× occupy → `find_slot("A") is None`, `occupancy == 1.0`), `test_release_and_locate`.
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer** met `dict[SlotLoc, str]` + omgekeerde index.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): yard slot allocation and occupancy`

---

### Task 5: Grid-routering en reserveringen

**Files:**
- Create: `packages/sim/src/terminal_sim/grid.py`
- Test: `tests/sim/test_grid.py`

**Interfaces:**
- Produces: `Cell = tuple[int, int]`; `class Grid`:
  - `__init__(self, cols: int, rows: int, cell_m: float)`
  - `neighbors(self, c: Cell) -> list[Cell]` — eenrichting: rij `y` even → alleen `x+1`; rij oneven → alleen `x-1`; verticaal `y±1` altijd toegestaan
  - `shortest_path(self, start: Cell, goal: Cell) -> list[Cell]` — BFS over `neighbors`, inclusief start en goal; `[]` als onbereikbaar
  - `try_reserve(self, c: Cell, owner: str) -> bool` — False als door ander gereserveerd; True (idempotent) als al door `owner`
  - `release(self, c: Cell, owner: str) -> None` — no-op als niet van `owner`
  - `owner_of(self, c: Cell) -> str | None`
  - `center(self, c: Cell) -> Position`

- [ ] **Stap 1: Tests** — `test_one_way_lanes` (rij 0: `(3,0)` buren = `{(4,0),(3,1)}`; rij 1: `(3,1)` buren = `{(2,1),(3,0),(3,2)}`), `test_path_respects_direction` (pad van `(5,0)` naar `(3,0)` gaat via rij 1), `test_reserve_conflict` (A reserveert `(2,2)`; B → False; A opnieuw → True; A release; B → True), `test_path_length_cells`.
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): grid routing with one-way lanes and cell reservations`

---

### Task 6: Equipment-basis en STS

**Files:**
- Create: `packages/sim/src/terminal_sim/equipment/{__init__,base,sts}.py`
- Test: `tests/sim/test_sts.py`

**Interfaces:**
- Produces (`base.py`): `class Equipment(salabim.Component)`:
  - `__init__(self, *, equipment_id: str, kind: Kind, position: Position, rng: random.Random, emit: Callable[[EquipmentEvent], None], env: salabim.Environment)`
  - attributen `state: State`, `position: Position`, `current_job: Job | None`, `energy_kwh: float`, `vendor: str = "SIM"`
  - `assign_job(self, job: Job) -> bool` — False tenzij `state == IDLE`; zet `BUSY`, `current_job`, activeert component
  - `cancel_job(self) -> bool` — False als geen job; zet job `FAILED`, emit `JOB_FAILED(reason="CANCELLED")`, state blijft `BUSY` tot `reset`
  - `reset(self) -> bool` — naar `IDLE` behalve vanuit `FAULT`
  - `set_state(self, s: State)` — emit `STATE_CHANGED`
  - `complete_job(self)` / `fail_job(self, reason: str)` — zetten status, emitten event, state `IDLE`
  - `snapshot(self) -> EquipmentSnapshot` met `extra` uit `self.extra() -> dict`
  - abstract `process(self)` (Salabim-proces dat `current_job` uitvoert)
- Produces (`sts.py`): `class STS(Equipment)`:
  - `__init__(..., lane_cells: tuple[Cell, Cell], grid: Grid)`
  - `extra()` → `berth_side: "WS"`, `spreader_height_m`, `trolley_position_m`, `handover_lane_occupied: bool`
  - `process`: wacht tot `grid.owner_of(lane_cells[0])` een AGV is (poll elke 5 s), dan `hold(rng.triangular(90, 150, 110))`, `energy_kwh += 5.0`, `complete_job()`
  - accepteert alleen `JobType.DISCHARGE` en `JobType.LOAD`, anders `assign_job` → False

- [ ] **Stap 1: Tests** (met `salabim.Environment(trace=False)` en een AGV-stand-in die de lane-cel reserveert)

```python
def test_sts_rejects_wrong_job_type(): ...  # TRANSPORT → False
def test_sts_discharge_cycle_time_in_bounds(env_with_sts):
    # reserveer lane cel als "AGV-01", assign DISCHARGE, env.run(till=200)
    # exact één JOB_COMPLETED tussen t=90 en t=150, state IDLE, energy == 5.0
def test_sts_waits_for_agv(env_with_sts):
    # geen reservering → na 300 s nog BUSY, geen event
def test_cancel_then_reset(env_with_sts):
    # cancel tijdens wachten → JOB_FAILED CANCELLED; state BUSY; reset → IDLE
```

- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer** `base.py` en `sts.py`.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): Equipment base component and STS behaviour`

---

### Task 7: AGV

**Files:**
- Create: `packages/sim/src/terminal_sim/equipment/agv.py`
- Test: `tests/sim/test_agv.py`

**Interfaces:**
- Produces: `class AGV(Equipment)`:
  - `__init__(..., start_cell: Cell, grid: Grid, resolve_cell: Callable[[str], Cell])` — `resolve_cell` vertaalt een locatiestring (`STS-01`, `TP/B/WS`) naar een doelcel (geleverd door World)
  - attributen `battery_pct: float = 100.0`, `loaded_container_id: str | None`, `route: list[Cell]`, `cell: Cell`
  - `extra()` → `battery_pct`, `loaded_container_id`, `route` (als lijst van `[x,y]`), `speed_mps`
  - `speed_mps` property: 6.0 leeg, 4.0 beladen
  - accepteert alleen `JobType.TRANSPORT`
  - `process`: `route = grid.shortest_path(cell, resolve_cell(job.to_loc))`; per volgende cel: `while not grid.try_reserve(next, id): hold(1.0)`; `hold(cell_m / speed_mps)`; `grid.release(prev, id)`; `cell = next`; `position = grid.center(next)`; `battery_pct -= 0.02 * cell_m`; `energy_kwh += 1.2 * cell_m / 1000`; bij aankomst `complete_job()` — **de doelcel blijft gereserveerd** tot de volgende job start of `reset`
  - `load(container_id: str)` / `unload() -> str` — aangeroepen door World bij overdracht
  - `cancel_job`: onderbreekt het proces; AGV blijft op `cell`, reservering blijft

- [ ] **Stap 1: Tests** — `test_speed_depends_on_load`, `test_transport_arrives_and_holds_cell` (van `(0,2)` naar `(4,0)`: `JOB_COMPLETED`, `grid.owner_of((4,0)) == "AGV-01"`, battery < 100), `test_two_agvs_never_share_cell` (twee AGV's over kruisende routes; na elke `env.step()` zijn alle `cell`-waarden uniek), `test_cancel_mid_route_keeps_cell_reserved`, `test_unreachable_goal_fails` (`JOB_FAILED reason="NO_ROUTE"`).
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): AGV routing, reservations, battery and energy`

---

### Task 8: ASC

**Files:**
- Create: `packages/sim/src/terminal_sim/equipment/asc.py`
- Test: `tests/sim/test_asc.py`

**Interfaces:**
- Produces: `class ASC(Equipment)`:
  - `__init__(..., block: BlockSpec, yard: Yard)`
  - attributen `gantry_bay: float` (huidige baypositie, start 1), `tp_ws_occupied: bool`, `tp_ls_occupied: bool = False`
  - constanten: `BAY_PITCH_M = 6.5`, `ROW_PITCH_M = 3.0`, `TIER_H_M = 3.0`, `GANTRY_MPS = 4.0`, `TROLLEY_MPS = 1.5`, `HOIST_MPS = 1.0`, `ENERGY_PER_MOVE_KWH = 2.5`
  - accepteert `STACK_IN` (from `TP/<block>/WS`, to `SlotLoc`) en `STACK_OUT` (from `SlotLoc`, to `TP/<block>/WS`)
  - cyclustijd = gantry naar bay 0 (TP) of slot + trolley naar row + hoist `tiers` omlaag en omhoog, twee keer (pick + place), met `TP` op bay 0, row 1, tier 1
  - `STACK_IN`: `yard.occupy(slot, container_id)`; bij `ValueError` → `fail_job("SLOT_OCCUPIED")` zonder tijd te verbruiken
  - `STACK_OUT`: `yard.release(slot)`; bij `KeyError` → `fail_job("SLOT_EMPTY")`
  - `extra()` → `block_id`, `gantry_position_m`, `ws_tp_occupied`, `ls_tp_occupied`

- [ ] **Stap 1: Tests** — `test_stack_in_occupies_slot_and_takes_time` (slot `A/10/3/1`: verwachte tijd = 2·(10·6.5/4 + 2·3.0/1.5) + 4·(3.0/1.0) ≈ 52.5 s ± 1 s; `yard.locate("C1") == slot`), `test_stack_in_occupied_slot_fails_immediately`, `test_stack_out_empty_fails`, `test_rejects_transport_job`.
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): ASC stacking with kinematic cycle time`

---

### Task 9: World en SimEquipmentPort

**Files:**
- Create: `packages/sim/src/terminal_sim/world.py`
- Test: `tests/sim/test_world.py`

**Interfaces:**
- Produces: `class World`:
  - `__init__(self, layout: Layout, scenario: Scenario)` — maakt `salabim.Environment(random_seed=scenario.seed, trace=False)`, `random.Random(scenario.seed)`, `Grid`, `Yard`, `n_sts` STS'en, `n_agv` AGV's, 4 ASC's
  - `port: EquipmentPort` (instantie van `SimEquipmentPort`)
  - `run(self, until: float) -> None`
  - `now(self) -> float`
  - `yard: Yard`, `grid: Grid`, `equipment: dict[str, Equipment]`
  - `transfer_sts_to_agv(self, sts_id: str, container_id: str) -> str | None` — vindt de AGV op een lane-cel van die STS, roept `agv.load`, geeft AGV-id
  - `transfer_agv_to_tp(self, agv_id: str) -> str` — `agv.unload()`
  - `resolve_cell(self, loc: str) -> Cell` — `STS-xx` → eerste vrije lane-cel (of eerste als beide bezet), `TP/x/WS` → `tp_ws_cell`
- Produces: `class SimEquipmentPort` (implementeert `EquipmentPort`) als dunne adapter over `World.equipment`; `subscribe` registreert callbacks die bij elk event synchroon worden aangeroepen

- [ ] **Stap 1: Tests** — `test_world_builds_from_baseline` (`len(port.equipment_ids()) == 12`, alle `IDLE`, `isinstance(world.port, EquipmentPort)`), `test_port_assign_and_event_roundtrip` (subscribe; assign TRANSPORT aan AGV-01 naar `TP/A/WS`; `world.run(300)`; callback ontving `JOB_COMPLETED` voor `AGV-01`; `snapshot("AGV-01").position == Position(50, 110)`), `test_port_unknown_id_raises` (`KeyError`).
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**. De `emit`-callback van elk Equipment wijst naar `SimEquipmentPort._dispatch`.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): World assembly and SimEquipmentPort adapter`

---

### Task 10: Storingen

**Files:**
- Create: `packages/sim/src/terminal_sim/faults.py`
- Modify: `packages/sim/src/terminal_sim/world.py` (FaultInjector per equipment als `faults[kind].mtbf_s > 0`)
- Test: `tests/sim/test_faults.py`

**Interfaces:**
- Produces: `class FaultInjector(salabim.Component)`:
  - `__init__(self, *, target: Equipment, params: FaultParams, rng: random.Random, env)`
  - proces: `hold(rng.expovariate(1/mtbf))`; als target niet al `FAULT`: onthoud lopende job, `target.fail_job("FAULT")` indien job, `target.set_state(FAULT)`; `hold(rng.expovariate(1/mttr))`; `target.set_state(IDLE)`; herhaal
  - `count: int` — aantal storingen

- [ ] **Stap 1: Tests** — `test_fault_cycle` (mtbf 100, mttr 10, seed vast: na `run(1000)` is `count >= 5` en de reeks `STATE_CHANGED`-events wisselt FAULT/IDLE af), `test_fault_fails_running_job` (AGV onderweg → `JOB_FAILED reason="FAULT"`), `test_no_injector_when_mtbf_zero` (baseline → geen `FaultInjector` in `world.env`).
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): fault injection with MTBF/MTTR`

---

### Task 11: ECS — werkorders en lossen

**Files:**
- Create: `packages/ecs/src/terminal_ecs/{__init__,workorders,dispatcher}.py`
- Create: `tests/ecs/fake_port.py`
- Test: `tests/ecs/test_dispatcher_discharge.py`

**Interfaces:**
- Produces (`workorders.py`):
  - `@dataclass(frozen=True) class WorkOrder: container_id: str; type: JobType; sts_id: str; yard_slot: str` — `type` ∈ {DISCHARGE, LOAD}
  - `class SlotProvider(Protocol): def next_slot(self, block: str, exclude: str | None = None) -> str | None` (geeft locatiestring of None als yard vol)
  - `class WorkOrderError(ValueError)` met `code: str` (`DUPLICATE_CONTAINER`, `UNKNOWN_STS`, `INVALID_SLOT`)
- Produces (`dispatcher.py`): `class Dispatcher`:
  - `__init__(self, port: EquipmentPort, slot_provider: SlotProvider, *, lookahead_per_sts: int = 2, watchdog_s: float = 600.0)` — roept `port.subscribe(self.on_event)`
  - `submit(self, orders: list[WorkOrder]) -> None` — valideert alles vooraf; raise `WorkOrderError` bij eerste fout, niets wordt ingepland
  - `on_event(self, ev: EquipmentEvent) -> None`
  - `tick(self) -> None` — probeert nieuwe toewijzingen; wordt door `on_event` aangeroepen en door de runner elke 60 s sim-tijd
  - `status(self) -> dict` — `{"pending": int, "active_jobs": int, "done": int, "failed": int, "replans": int, "equipment": {id: state}}`
  - `jobs: dict[str, Job]` (actief), `completed: list[str]` (container_ids in stack)
  - `on_transfer: Callable[[str, str, str], None] | None` — hook `(kind, equipment_id, container_id)` met kind ∈ {`"sts_to_agv"`, `"agv_to_tp"`}; de runner koppelt deze aan `World.transfer_*`
- Produces (`fake_port.py`): `class FakePort` — in-memory `EquipmentPort` met `complete(equipment_id)` en `fail(equipment_id, reason)` om events te forceren, `advance(dt)` voor `now()`, registreert alle `assign_job`-aanroepen in `calls: list[tuple[str, Job]]`

**Losregels** (spec §5): per STS een FIFO van DISCHARGE-orders; staat: `AWAIT_AGV → AGV_EN_ROUTE → AT_STS → DISCHARGING → TO_YARD → STACKING → DONE`. Kies dichtstbijzijnde `IDLE` AGV op `snapshot().position`. Max `lookahead_per_sts` AGV's in staten `AGV_EN_ROUTE`/`AT_STS` per STS. Job-id's: `f"{order.container_id}-{JobType}-{n}"`.

- [ ] **Stap 1: Tests** (alle met `FakePort` met 1 STS, 2 AGV, 1 ASC)

```python
def test_submit_rejects_duplicate_container(): ...  # WorkOrderError code DUPLICATE_CONTAINER, port.calls == []
def test_submit_rejects_unknown_sts(): ...
def test_discharge_happy_path():
    # submit 1 order → assign TRANSPORT aan dichtstbijzijnde AGV naar STS-01
    # fake.complete("AGV-01") → assign DISCHARGE aan STS-01
    # fake.complete("STS-01") → on_transfer("sts_to_agv",...) aangeroepen; assign TRANSPORT AGV-01 → TP/A/WS
    # fake.complete("AGV-01") → on_transfer("agv_to_tp",...); assign STACK_IN aan ASC-A met to_loc == order.yard_slot
    # fake.complete("ASC-A") → completed == ["C1"], status()["active_jobs"] == 0
def test_lookahead_limits_agvs_per_sts():
    # 5 orders, 6 AGV's idle → precies 2 TRANSPORT-jobs naar STS-01 uitgegeven
def test_slot_occupied_asks_provider():
    # fake.fail("ASC-A","SLOT_OCCUPIED") → slot_provider.next_slot aangeroepen met exclude=oude slot → nieuwe STACK_IN
```

- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer** `workorders.py`, `dispatcher.py` (alleen DISCHARGE-pad), `fake_port.py`.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(ecs): work orders, FakePort and discharge dispatch`

---

### Task 12: ECS — laden, herplanning, watchdog

**Files:**
- Modify: `packages/ecs/src/terminal_ecs/dispatcher.py`
- Test: `tests/ecs/test_dispatcher_load.py`, `tests/ecs/test_dispatcher_recovery.py`

**Interfaces:**
- Consumes: Taak 11
- Produces: geen nieuwe publieke namen; gedrag:
  - LOAD-pad: `STACK_OUT` (ASC) → `TRANSPORT` AGV naar `TP/<block>/WS` → `on_transfer("tp_to_agv", ...)` → `TRANSPORT` naar STS → `LOAD` → `on_transfer("agv_to_sts", ...)`; AGV vrij
  - `JOB_FAILED` met reason ≠ `SLOT_OCCUPIED`/`SLOT_EMPTY`: order terug vooraan in FIFO van die STS, `replans += 1`, apparaat uitgesloten tot `STATE_CHANGED → IDLE`
  - `STATE_CHANGED → FAULT`: zelfde als hierboven voor de lopende job van dat apparaat
  - Watchdog in `tick()`: AGV-job met `now - last_progress > watchdog_s` → `port.cancel_job`, `port.reset`, order terug in FIFO, `replans += 1`. `last_progress` = laatste event of positieverandering van die AGV
  - `SlotProvider.next_slot` geeft `None` → job `FAILED(YARD_FULL)`, order verwijderd, `status()["failed"] += 1`

- [ ] **Stap 1: Tests** — `test_load_happy_path` (spiegel van discharge, 5 stappen, eindigt met `completed == ["C9"]`), `test_fault_during_transport_replans` (fake.fail AGV-01 "FAULT" → nieuwe TRANSPORT aan AGV-02, `replans == 1`, AGV-01 krijgt geen job tot STATE_CHANGED IDLE), `test_watchdog_cancels_stuck_agv` (geen events, `advance(601)`, `tick()` → `cancel_job` en `reset` op die AGV in `fake.calls_cancel`), `test_yard_full_fails_order`.
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(ecs): load flow, replanning on failure and fault, AGV watchdog`

---

### Task 13: TOS-stub

**Files:**
- Create: `packages/tos_stub/src/terminal_tos_stub/{__init__,generator,allocator}.py`
- Test: `tests/tos_stub/test_generator.py`, `tests/tos_stub/test_allocator.py`

**Interfaces:**
- Produces (`allocator.py`): `class SimpleSlotAllocator` (implementeert `SlotProvider`):
  - `__init__(self, blocks: list[BlockSpec])` — eigen boekhouding, los van `Yard`
  - `next_slot(self, block: str, exclude: str | None = None) -> str | None` — zelfde vulvolgorde als `Yard.find_slot`; markeert teruggegeven slot als uitgegeven; bij vol blok → probeer andere blokken op volgorde A–D; `None` als alles vol
  - `release(self, slot: str) -> None`
- Produces (`generator.py`): `generate_workorders(scenario: Scenario, layout: Layout, allocator: SimpleSlotAllocator, rng: random.Random) -> list[WorkOrder]`:
  - `n_containers` orders; `round(n_containers * discharge_share)` DISCHARGE, rest LOAD
  - container-id's `C0001`…; STS round-robin over `STS-01..n_sts`; blok round-robin A–D; `yard_slot = allocator.next_slot(block)`
  - LOAD-orders verwijzen naar slots die de generator vooraf als "bezet" markeert (de runner vult de `Yard` daarmee vóór de run)
  - `prefill(yard: Yard, orders: list[WorkOrder]) -> None` — zet containers van LOAD-orders in de yard

- [ ] **Stap 1: Tests** — `test_generator_counts_and_roundrobin` (200 orders, 100 per STS, 50 per blok, allemaal DISCHARGE bij `discharge_share 1.0`), `test_generator_deterministic` (zelfde seed → identieke lijst), `test_allocator_spills_to_next_block` (960× A → 961e komt uit B), `test_prefill_puts_load_containers_in_yard`.
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(tos_stub): work order generator and slot allocator`

---

### Task 14: KPI-collector

**Files:**
- Create: `packages/sim/src/terminal_sim/kpi.py`
- Test: `tests/sim/test_kpi.py`

**Interfaces:**
- Produces: `class KpiCollector`:
  - `__init__(self, port: EquipmentPort, dispatcher: Dispatcher)` — subscribe op port-events; leest `dispatcher.status()`
  - `sample(self) -> None` — elke 60 s door runner aangeroepen: registreert per equipment `state` en per AGV of hij wacht (BUSY zonder positieverandering)
  - `finish(self, duration_s: float) -> dict[str, float]` met sleutels: `moves_per_hour_total`, `moves_per_hour_STS-01`, `moves_per_hour_STS-02`, `agv_wait_s_mean`, `agv_travel_s_mean`, `utilisation_STS`, `utilisation_AGV`, `utilisation_ASC`, `vessel_turnaround_s` (tijd van eerste job tot laatste `completed`), `faults`, `replans`, `energy_kwh_per_move`, `containers_done`, `containers_failed`
  - `to_csv_row(self, path: Path, run_id: int, kpis: dict) -> None` — append met header als bestand nieuw

- [ ] **Stap 1: Tests** — met `FakePort` + `Dispatcher`: `test_moves_per_hour` (10 STS-completions in 3600 s → `moves_per_hour_total == 10.0`), `test_utilisation_from_samples` (3 samples BUSY, 1 IDLE → `utilisation_AGV == 0.75`), `test_csv_header_once`.
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): KPI collector and CSV output`

---

### Task 15: Runner CLI

**Files:**
- Create: `packages/sim/src/terminal_sim/run.py`
- Test: `tests/sim/test_run.py`

**Interfaces:**
- Produces:
  - `run_once(scenario: Scenario, layout: Layout, seed: int) -> dict[str, float]` — bouwt World, allocator, generator, Dispatcher (met `on_transfer` gekoppeld aan `World.transfer_*`), KpiCollector; `prefill`; `submit`; loop: `world.run(until=t+60); dispatcher.tick(); kpi.sample()` tot `duration_s` of tot `status()["pending"] == 0 and active_jobs == 0`; retourneert `kpi.finish(...)`
  - `run_replications(scenario_path: Path, replications: int, out_dir: Path) -> Path` — seeds `scenario.seed + i`; schrijft `out_dir/<name>.csv` en `out_dir/<name>-summary.json` met per KPI `mean`, `p5`, `p95`
  - `main(argv: list[str] | None = None) -> int` — `python -m terminal_sim.run scenarios/baseline.yaml --replications 10 --out out/`
- Alle `on_transfer`-hooks: `sts_to_agv` → `World.transfer_sts_to_agv`; `agv_to_tp` → `World.transfer_agv_to_tp`; `tp_to_agv` → `agv.load(container_id)`; `agv_to_sts` → `agv.unload()`

- [ ] **Stap 1: Tests** — `test_run_once_small` (scenario met 20 containers, 2 AGV, 1 STS via tmp yaml → `containers_done == 20`, `containers_failed == 0`, `moves_per_hour_total > 0`), `test_reproducible` (twee keer `run_once` zelfde seed → identieke dict), `test_cli_writes_csv_and_summary` (replications 2 → csv met 2 datarijen, json met `moves_per_hour_total.mean`).
- [ ] **Stap 2: Run** → FAIL
- [ ] **Stap 3: Implementeer**. Stop-conditie en `duration_s` als harde bovengrens.
- [ ] **Stap 4: Run** → PASS
- [ ] **Stap 5: Commit** `feat(sim): scenario runner with replications and summary`

---

### Task 16: End-to-end en architectuurbewaking

**Files:**
- Test: `tests/e2e/test_baseline.py`, `tests/e2e/test_scenarios_compare.py`, `tests/test_architecture.py`
- Create: `README.md`

- [ ] **Stap 1: Tests**

```python
# tests/e2e/test_baseline.py
def test_baseline_200_containers_complete():
    k = run_once(load_scenario("scenarios/baseline.yaml"), load_layout("terminal.yaml"), seed=1)
    assert k["containers_done"] == 200 and k["containers_failed"] == 0
    assert 20 <= k["moves_per_hour_STS-01"] <= 45     # plausibiliteit, geen exacte eis

# tests/e2e/test_scenarios_compare.py
def test_fewer_agvs_lower_throughput():
    base = run_once(baseline, layout, 1); agv4 = run_once(load_scenario("scenarios/agv4.yaml"), layout, 1)
    assert agv4["moves_per_hour_total"] < base["moves_per_hour_total"]

def test_faults_scenario_completes_with_replans():
    k = run_once(load_scenario("scenarios/faults.yaml"), layout, 1)
    assert k["faults"] > 0 and k["replans"] > 0 and k["containers_done"] == 200

# tests/test_architecture.py
def test_ecs_never_imports_sim():
    for p in Path("packages/ecs/src").rglob("*.py"):
        assert "terminal_sim" not in p.read_text()
```

- [ ] **Stap 2: Run** `uv run pytest -v` → FAIL op de e2e-tests (of PASS; dan alleen stap 4–5)
- [ ] **Stap 3: Repareer** wat de e2e-tests blootleggen; pas plausibiliteitsgrenzen alleen aan met onderbouwing in de commitmessage.
- [ ] **Stap 4: README** — hoe installeren (`uv sync`), hoe draaien (`uv run python -m terminal_sim.run scenarios/baseline.yaml --replications 10 --out out/`), wat de KPI-kolommen betekenen, verwijzing naar spec en Plan B.
- [ ] **Stap 5: Run** `uv run pytest -v` → alles PASS
- [ ] **Stap 6: Commit** `test: end-to-end scenarios and architecture guard; docs: README`

---

## Zelfreview

- **Spec-dekking:** §1 succescriteria → Taak 15/16; §3 model → Taak 1/2 (OPC UA-namespace zelf is Plan B); §4 wereld → Taak 3–10; §5 ECS → Taak 11/12; §6 TOS-stub → Taak 13 (HTTP-API is Plan B); §7 gateway, §8 viewer, §2 Docker → Plan B; §9 scenario's/KPI's → Taak 3/14/15; §11 fouten → Taak 8/10/12; §12 testen → per taak + Taak 16; §13 repo → bestandsstructuur.
- **Typeconsistentie:** `EquipmentPort.now()` gebruikt door Dispatcher-watchdog en KpiCollector; `on_transfer`-kinds (`sts_to_agv`, `agv_to_tp`, `tp_to_agv`, `agv_to_sts`) identiek in Taak 11, 12, 15; `SlotProvider.next_slot(block, exclude)` identiek in Taak 11, 13.
- **Review Focus:** alle vijf hebben een test in de genoemde taak.
