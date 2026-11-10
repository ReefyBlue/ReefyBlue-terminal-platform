# Plan B2a — ECS als service: implementatieplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** De ECS draait als zelfstandig FastAPI-proces (werkorders-API, status/KPI's/equipment, websocket-eventstroom) tegen de gateway; TOS-stub en runner worden HTTP-clients.

**Architecture:** Nieuw pakket `terminal_ecs_service` met `EcsCore` (één asyncio-loop: `ReconnectingPort` → bevroren `Dispatcher` + `KpiCollector`, `step()` = drain + tick/sample op 60 s-grenzen sim-tijd) en `create_app(core)` (FastAPI). `KpiCollector` verhuist naar pakket `terminal_kpi`. `terminal_tos_stub` krijgt `occupy`, een httpx-`EcsClient` en een `submit`-CLI; de runner krijgt `run_once_http`.

**Tech Stack:** Python 3.13, FastAPI, uvicorn, httpx, asyncua 2.1.0 (via bestaande `OpcUaEquipmentPort`), pytest-asyncio, uv workspace.

**Spec:** `docs/superpowers/specs/2026-11-10-plan-b2a-ecs-service-design.md`

## Global Constraints

- `terminal_ecs` (dispatcher/orders/workorders) blijft byte-identiek: hash-fixture `tests/fixtures/ecs_sources.sha256`.
- Pakketregels (architectuurtest): kpi→model+ecs; tos_stub→model+ecs; ecs_service→model+ecs+opcua+kpi+tos_stub; runner→alles; overige ongewijzigd.
- Nieuwe dependencies alleen: `fastapi`, `uvicorn[standard]`, `httpx`.
- `SAMPLE_INTERVAL_S = 60` s sim-tijd per tick; `DRAIN_POLL_S = 0.005` s wandklok; `CONNECT_RETRY_S = 2.0`; runner health-timeout 30 s; WS-wachtrij per client 1000, drop-warning max. 1 per client per minuut.
- Alle endpoints `async def`; dispatcher/KPI alleen op de loop-thread; geen locks.
- API-vormen exact zoals spec §3; WS-berichten exact zoals spec §5 (`type` ∈ status/equipment/job/link).
- Tests: `uv run pytest -q` (default sluit `e2e_opcua` uit); salabim `Environment(trace=False, yieldless=False)`.
- Commit per taak; trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` + `Claude-Session: https://claude.ai/code/session_011Q6oL3tzJGQycNuMVuzTtk`.

## Review Focus

1. `POST /workorders` met een batch waarvan order 3 van 5 een dubbele container is: niets aangenomen, 422 noemt die container — Task 4 `test_workorders_batch_rejected_atomically`.
2. Gatewayverbinding valt weg midden in een run en komt terug: ECS behoudt open orders, `link` false→true op WS, daarna worden events weer geleverd — Task 3 `test_reconnecting_port_swaps_and_redelivers`, Task 4 `test_ws_link_messages_on_reconnect`.
3. `GET /kpis` vóór de eerste tick en vóór verbinding: geldig antwoord (nullen / 503), geen 500 — Task 4 `test_kpis_before_first_tick_and_without_connection`.
4. Twee WS-clients, één leest niet: de trage client krijgt drops, de andere alles — Task 3 `test_broadcaster_drops_only_for_slow_client`.
5. `yard_slot` met een onbekend blok (`Z/1/1/1`) of ongeldige syntaxis: 422 `INVALID_SLOT`, allocator ongewijzigd — Task 4 `test_workorders_invalid_slot_422`.

---

### Task 1: Pakket `terminal_kpi`

**Files:**
- Create: `packages/kpi/pyproject.toml`, `packages/kpi/src/terminal_kpi/__init__.py`, `packages/kpi/src/terminal_kpi/kpi.py` (verplaatst uit `packages/runner/src/terminal_runner/kpi.py`, `git mv`)
- Modify: `pyproject.toml` (dev-group + `[tool.uv.sources]` + `pytest.pythonpath` → `packages/kpi/src`), `packages/runner/pyproject.toml` (dependency `terminal-kpi`), `packages/runner/src/terminal_runner/run.py` (import), `tests/test_architecture.py` (`PACKAGES` + `ALLOWED["kpi"] = {"terminal_model", "terminal_ecs"}`)
- Test: `tests/kpi/test_kpi.py` (verplaatst uit `tests/runner/test_kpi.py`, `git mv`)

**Interfaces:**
- Produces: `terminal_kpi.kpi.KpiCollector` ongewijzigde constructor `KpiCollector(port, dispatcher, *, faults_total=None)`; nieuw `snapshot(duration_s: float) -> dict[str, float]` (zelfde sleutels/volgorde als `finish`, inclusief `sim_elapsed_s = duration_s`), `finish(duration_s)` = `snapshot(duration_s)` (bestaand gedrag, `sim_elapsed_s` wordt nu ook door `finish` gezet — runner zet hem daarna nogmaals, harmless). `SAMPLE_INTERVAL_S` blijft in deze module.

- [ ] **Step 1: Voeg aan `tests/kpi/test_kpi.py` toe**

```python
def test_snapshot_is_non_destructive_and_matches_finish():
    ...  # bestaande opbouw uit test_moves_per_hour hergebruiken
    a = kpi.snapshot(3600.0); b = kpi.snapshot(3600.0)
    assert a == b and kpi.finish(3600.0) == a
    assert a["sim_elapsed_s"] == 3600.0
```

- [ ] **Step 2: Run** `uv run pytest tests/kpi -q` → FAIL (module/attribuut ontbreekt).
- [ ] **Step 3: Verplaats module en test, maak pakket, update imports/pyproject/architectuurtest; implementeer `snapshot`.** `uv sync` na pyproject-wijziging.
- [ ] **Step 4: Run** `uv run pytest tests/kpi tests/runner tests/test_architecture.py -q` → PASS; `tests/e2e/test_plan_a_equivalence.py` → PASS (sleutelvolgorde van de fixture mag niet veranderen: `sim_elapsed_s` staat daar al als laatste sleutel — controleer; zo niet, laat `snapshot` hem niet toevoegen en houd de runner-toewijzing).
- [ ] **Step 5: Commit** `refactor(kpi): KpiCollector in own package terminal_kpi with non-destructive snapshot`

### Task 2: TOS-stub als HTTP-client

**Files:**
- Modify: `packages/tos_stub/src/terminal_tos_stub/allocator.py`, `packages/tos_stub/pyproject.toml` (dependency `httpx>=0.27`)
- Create: `packages/tos_stub/src/terminal_tos_stub/client.py`, `packages/tos_stub/src/terminal_tos_stub/submit.py`
- Test: `tests/tos_stub/test_allocator.py` (bestaand of nieuw), `tests/tos_stub/test_client.py`, `tests/tos_stub/test_submit.py`

**Interfaces:**
- Produces: `SimpleSlotAllocator.occupy(slot: str) -> None` (idempotent; `KeyError` bij onbekend blok; daarna geeft `next_slot` dat slot niet meer uit).
- Produces `client.py`: `class EcsClient` met `__init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None, timeout_s: float = 5.0)`; `post_workorders(orders: list[WorkOrder]) -> int` (aantal geaccepteerd; `WorkOrderRejected(errors: list[dict])` bij 422; `httpx.HTTPStatusError` bij andere 4xx/5xx); `status() -> dict`; `kpis() -> dict[str, float]`; `equipment() -> list[dict]`; `health() -> bool`; `wait_healthy(timeout_s: float, poll_s: float = 0.5) -> bool`; `close()`; contextmanager. Serialisatie van `WorkOrder`: `{"container_id","type": type.value,"sts_id","yard_slot"}`.
- Produces `submit.py`: `main(argv) -> int`: `scenario.yaml --layout terminal.yaml --ecs http://localhost:8000 [--seed N]`; genereert orders met `generate_workorders` + `SimpleSlotAllocator` (zelfde argumenten als `terminal_runner.run.make_workorders`, maar zonder runner-import: herhaal de 6 regels), `wait_healthy(30)` anders exit 1 met melding, POST, print `{"accepted": n}`; 422 → exit 2 met de fouten op stderr.

- [ ] **Step 1: Tests**

```python
def test_occupy_marks_slot_used():
    a = SimpleSlotAllocator({"A": (2, 1, 1)})
    a.occupy("A/1/1/1"); a.occupy("A/1/1/1")
    assert a.next_slot("A") == "A/2/1/1"
def test_occupy_unknown_block_raises(): ... KeyError

# client: httpx.MockTransport die paden/bodies vastlegt
def test_post_workorders_serialises_and_returns_accepted(): ... body == [{"container_id":"C1","type":"DISCHARGE","sts_id":"STS-01","yard_slot":"A/1/1/1"}]; return 202 {"accepted":1} → 1
def test_post_workorders_422_raises_with_errors(): ... WorkOrderRejected(errors=[{...}])
def test_wait_healthy_polls_until_200(): ... eerste 2 antwoorden 503, dan 200 → True; 3 calls
def test_wait_healthy_times_out(): ... altijd 503, timeout_s=0.05 → False

# submit: monkeypatch EcsClient door een fake; main(["scenarios/baseline.yaml","--ecs","http://x"]) → 0 en fake kreeg 200 orders
```

- [ ] **Step 2: Run** `uv run pytest tests/tos_stub -q` → FAIL.
- [ ] **Step 3: Implementeer.** `uv sync` na pyproject-wijziging.
- [ ] **Step 4: Run** `uv run pytest tests/tos_stub tests/test_architecture.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(tos_stub): slot occupy, httpx EcsClient and submit CLI`

### Task 3: `terminal_ecs_service` kern — ReconnectingPort, broadcaster, EcsCore

**Files:**
- Create: `packages/ecs_service/pyproject.toml` (deps: terminal-model, terminal-ecs, terminal-opcua, terminal-kpi, terminal-tos-stub, `fastapi>=0.115`, `uvicorn[standard]>=0.30`), `packages/ecs_service/src/terminal_ecs_service/{__init__,port,events,core}.py`
- Modify: `pyproject.toml` (dev-group, sources, pythonpath `packages/ecs_service/src`), `tests/test_architecture.py` (`ALLOWED["ecs_service"] = {"terminal_model","terminal_ecs","terminal_opcua","terminal_kpi","terminal_tos_stub"}`)
- Test: `tests/ecs_service/test_port.py`, `tests/ecs_service/test_events.py`, `tests/ecs_service/test_core.py` (gebruik `tests/ecs/fake_port.py::FakePort` en `FakeSlots`; `tests/ecs` op `pythonpath` zetten of relatief importeren zoals bestaande tests)

**Interfaces:**
- Produces `port.py`: `class ReconnectingPort` implementeert `EquipmentPort`; `__init__(self)`; `bind(port: EquipmentPort) -> None` (zet huidige port, abonneert alle bewaarde callbacks); `unbind() -> None` (geen port; `now()` geeft laatst bekende waarde; `equipment_ids()/snapshot()` geven laatst bekende snapshots terug — bewaard bij elke `snapshot`-aanroep en bij `bind` voor alle ids; `assign_job/cancel_job/reset` → `False` + warning); `connected: bool`; `subscribe(cb)` bewaart cb en abonneert op de huidige port als die er is; alle methoden delegeren anders 1-op-1.
- Produces `events.py`: `class Broadcaster` met `QUEUE_SIZE = 1000`, `WARN_INTERVAL_S = 60.0`; `register() -> asyncio.Queue`; `unregister(q)`; `publish(message: dict) -> None` (sync; `put_nowait`, bij `QueueFull` droppen en max. één warning per client per `WARN_INTERVAL_S`); `clients: int`.
- Produces `core.py`: `SAMPLE_INTERVAL_S` (import uit `terminal_kpi.kpi`), `DRAIN_POLL_S = 0.005`, `CONNECT_RETRY_S = 2.0`;
  `class EcsCore`: `__init__(self, layout: LayoutLike, *, connect: Callable[[], EquipmentPort], broadcaster: Broadcaster | None = None)` waarbij `connect` een verbonden port teruggeeft of `ConnectionError` gooit (productie: lambda die `OpcUaEquipmentPort(gateway)` maakt en `.connect()` aanroept); attributen `port: ReconnectingPort`, `dispatcher: Dispatcher | None` (aangemaakt bij eerste succesvolle verbinding, want `Dispatcher.__init__` leest de vloot), `kpi: KpiCollector | None`, `allocator: SimpleSlotAllocator` (uit layout zoals `make_allocator`), `broadcaster`, `t0: float`, `next_tick: float`, `tick_count: int`, `started_at: float` (monotonic), `connected -> bool`.
  Methoden: `async connect_once() -> bool` (één poging: `connect()` → `port.bind`, dispatcher/kpi aanmaken bij eerste keer, `t0 = next_tick = port.now()`, `next_tick += SAMPLE_INTERVAL_S`, publish `link` true; bij `ConnectionError` → False); `step() -> None` (sync: `port.drain_events()`; `while port.now() >= next_tick: dispatcher.tick(); kpi.sample(); next_tick += 60; tick_count += 1; publish_status(); publish_new_jobs()`; `ConnectionError` → `on_link_lost()`); `on_link_lost()` (port.unbind, publish `link` false); `async run() -> None` (lus: zolang niet verbonden `await connect_once()` of `sleep(CONNECT_RETRY_S)`; anders `step()`; `await asyncio.sleep(DRAIN_POLL_S)`); `submit(orders: list[WorkOrder]) -> None` (`allocator.occupy` per order, `dispatcher.submit`, `dispatcher.tick()`, `publish_new_jobs()`); `status() -> dict` (`dispatcher.status()` of lege telling als geen dispatcher, + `sim_time`, `connected`, `uptime_s`, `tick_count`); `kpis() -> dict[str, float]` (`kpi.snapshot(port.now() - t0)`; zonder dispatcher: alle sleutels 0.0 via een lege collector is niet mogelijk → geef `{}` en laat de API 503 geven); `equipment() -> list[dict]`; `publish_status()`; `publish_new_jobs()` (diff op `dispatcher.job_log()`-sleutels, bewaar `_seen_jobs: set[str]`). Equipment-events uit de port gaan via `port.subscribe(self._on_equipment_event)` naar `broadcaster.publish({"type":"equipment",...})`.
  Berichtvormen exact spec §5.

- [ ] **Step 1: Tests**

```python
# test_port.py
def test_reconnecting_port_swaps_and_redelivers():
    rp = ReconnectingPort(); seen = []; rp.subscribe(seen.append)
    p1 = FakePort({...}); rp.bind(p1); p1.set_state("AGV-01", State.BUSY); assert len(seen) == 1
    rp.unbind(); assert rp.connected is False and rp.now() == p1.now()
    assert rp.snapshot("AGV-01").state is State.BUSY  # laatst bekende
    assert rp.assign_job("AGV-01", job) is False
    p2 = FakePort({...}); rp.bind(p2); p2.set_state("AGV-01", State.IDLE); assert len(seen) == 2
def test_reconnecting_port_satisfies_protocol(): isinstance(ReconnectingPort(), EquipmentPort)

# test_events.py
async def test_broadcaster_drops_only_for_slow_client(caplog):
    b = Broadcaster(); fast = b.register(); slow = b.register()
    for i in range(Broadcaster.QUEUE_SIZE + 5): b.publish({"type":"status","i":i})
    # fast leest alles mee in een taak; slow leest niets
    assert slow.qsize() == Broadcaster.QUEUE_SIZE and fast_received == QUEUE_SIZE + 5
    assert sum("dropped" in r.message for r in caplog.records) == 1  # één warning

# test_core.py (FakePort met 1 STS, 1 AGV, 1 ASC; connect=lambda: fake)
async def test_step_ticks_on_60s_boundaries(): await core.connect_once(); fake.advance(59); core.step(); assert core.tick_count == 0; fake.advance(1); core.step(); assert core.tick_count == 1; fake.advance(120); core.step(); assert core.tick_count == 3
async def test_connect_once_false_on_connection_error(): connect raises ConnectionError → False, core.connected False
async def test_step_on_connection_error_unbinds_and_publishes_link(): fake.drain_events raises ConnectionError (FakePort subclass) → core.connected False; broadcaster kreeg {"type":"link","connected":False,...}
async def test_submit_occupies_slots_and_publishes_jobs(): core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")]) → allocator.next_slot("A") != "A/1/1/1"; een "job"-bericht gepubliceerd (dispatcher wees de STS toe)
async def test_status_and_kpis_without_connection(): status()["connected"] is False, "open" == 0; kpis() == {}
```

- [ ] **Step 2: Run** `uv run pytest tests/ecs_service -q` → FAIL.
- [ ] **Step 3: Implementeer** pakket, `uv sync`, drie modules, architectuurtest-regel.
- [ ] **Step 4: Run** `uv run pytest tests/ecs_service tests/test_architecture.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(ecs_service): ReconnectingPort, Broadcaster and EcsCore loop`

### Task 4: FastAPI-app, schema's, CLI

**Files:**
- Create: `packages/ecs_service/src/terminal_ecs_service/{schemas,app,serve}.py`
- Test: `tests/ecs_service/test_app.py`

**Interfaces:**
- Consumes: `EcsCore`, `Broadcaster` (Task 3), `EcsClient` wire-format (Task 2).
- Produces `schemas.py` (Pydantic v2): `WorkOrderIn(container_id: str, type: Literal["DISCHARGE","LOAD"], sts_id: str, yard_slot: str)`, `WorkOrderErrorOut(container_id, code, message)`, `AcceptedOut(accepted: int)`, `HealthOut(connected: bool, sim_time: float | None)`.
- Produces `app.py`: `create_app(core: EcsCore) -> FastAPI` met lifespan die `core.run()` als taak start/stopt (uit te zetten via `create_app(core, run_loop=False)` voor tests die `step()` zelf aanroepen); routes exact spec §3: `POST /workorders` (503 als `not core.connected`; validatie: dubbele container binnen batch of t.o.v. `core.dispatcher`-bekende orders → `DUPLICATE_CONTAINER`; `sts_id` niet in vloot → `UNKNOWN_STS`; `parse_location` faalt of blok onbekend in `core.allocator` → `INVALID_SLOT`; alles verzamelen, bij ≥1 fout 422 `{"errors":[...]}`, anders `core.submit`, 202), `GET /status`, `GET /kpis` (503 zonder dispatcher), `GET /equipment`, `GET /health` (200/503), `WS /events` (eerst `status`-bericht, dan `queue.get()`-lus; disconnect → `unregister`).
- Produces `serve.py`: `main(argv) -> int`: `--gateway opc.tcp://localhost:4840 --layout terminal.yaml --host 0.0.0.0 --port 8000 --log-level info`; `configure_json_logging()`; `core = EcsCore(load_layout(layout), connect=<OpcUaEquipmentPort fabriek>)`; `uvicorn.run(create_app(core), host, port, log_config=None)`. SIGTERM → uvicorn stopt, lifespan annuleert `run()`, port sluit.

- [ ] **Step 1: Tests** (`FakePort`, `create_app(core, run_loop=False)`, `httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")`; WS via `fastapi.testclient.TestClient(app).websocket_connect("/events")`)

```python
async def test_health_503_then_200(): vóór connect_once → 503 {"connected": false}; na → 200 {"connected": true, "sim_time": 0.0}
async def test_workorders_accepted_202(): 1 geldige order → 202 {"accepted": 1}; GET /status open == 1
async def test_workorders_batch_rejected_atomically(): 5 orders, nr 3 dubbel (zelfde container als nr 1) → 422, errors == [{"container_id":"C1","code":"DUPLICATE_CONTAINER",...}]; GET /status open == 0
async def test_workorders_invalid_slot_422(): "Z/1/1/1" en "garbage" → INVALID_SLOT per order; allocator.next_slot("A") == "A/1/1/1"
async def test_workorders_unknown_sts_422(): "STS-99" → UNKNOWN_STS
async def test_workorders_503_without_connection()
async def test_kpis_before_first_tick_and_without_connection(): zonder connect → 503; na connect zonder tick → 200 met containers_done == 0.0 en sim_elapsed_s == 0.0
async def test_equipment_lists_fleet(): ids en kinds van de FakePort
def test_ws_first_message_is_status_then_events(): websocket_connect → eerste bericht type status; fake.set_state → core.step() → bericht type equipment met kind STATE_CHANGED
def test_ws_link_messages_on_reconnect(): core.on_link_lost() → {"type":"link","connected":false}; connect_once → {"type":"link","connected":true}
def test_serve_main_builds_app(monkeypatch): uvicorn.run gepatcht; main(["--gateway","opc.tcp://x","--layout","terminal.yaml","--port","1"]) == 0 en run aangeroepen met host/port
```

- [ ] **Step 2: Run** → FAIL.  **Step 3: Implementeer.**  **Step 4: Run** `uv run pytest tests/ecs_service -q` → PASS.
- [ ] **Step 5: Commit** `feat(ecs_service): FastAPI app (workorders, status, kpis, equipment, health, WS events) and serve CLI`

### Task 5: Runner `--port http` en in-process-equivalentie

**Files:**
- Modify: `packages/runner/src/terminal_runner/run.py`, `packages/runner/pyproject.toml` (deps `terminal-kpi`, `terminal-tos-stub` al aanwezig? anders toevoegen)
- Test: `tests/runner/test_run_http.py`, `tests/e2e/test_service_equivalence.py`

**Interfaces:**
- Consumes: `EcsClient` (Task 2), `EcsCore`/`create_app` (Tasks 3–4), bestaande `_connect`, `_check_fleet`-logica, `KpiCollector.to_csv_row`.
- Produces: `run_once_http(scenario, layout, seed, *, ecs: str, gateway: str, client: EcsClient | None = None) -> dict[str, float]`: `client or EcsClient(ecs)`; `wait_healthy(30.0)` anders `SystemExit(1)` met melding; vlootcontrole op `client.equipment()` (zelfde melding als `_check_fleet`, gedeelde hulpfunctie `_fleet_mismatch(got: dict[Kind,int], scenario, layout) -> str | None`); `port = _connect(gateway)` alleen voor `prefill_yard`/`start_sim`/`stop_sim`; orders via `make_workorders` → `client.post_workorders`; `start_sim`; poll `client.status()` elke `STATUS_POLL_S = 0.5` s wandklok tot `open == 0` of `sim_time - t0 >= duration_s` (timed_out) of stilstand (`sim_time` 120 s wandklok onveranderd → `RuntimeError("sim time stalled")`); `kpis = client.kpis()`; `sim_elapsed_s`/`timed_out` zetten; `stop_sim` (best effort in finally zoals `run_once_opcua`). CLI: `--port http`, `--ecs http://localhost:8000`; `--replications > 1` met http → zelfde parser-fout als opcua.

- [ ] **Step 1: Tests**
  - `test_run_http.py` met `EcsClient(transport=httpx.MockTransport(handler))` die een scripted ECS speelt (health 200, equipment = vloot, workorders 202, status eerst open 3 → dan open 0 met oplopende sim_time, kpis vast dict) en een gepatchte `_connect` die een fake port met `prefill_yard/start_sim/stop_sim` teruggeeft: resultaat bevat de kpis + `timed_out == 0.0`; `stop_sim` aangeroepen; variant waarbij health nooit 200 → `SystemExit` code 1; variant vloot mismatch → `SystemExit` 1.
  - `tests/e2e/test_service_equivalence.py` (marker `e2e`): World in-process (`World(layout, scenario)` baseline seed 1), `core = EcsCore(layout, connect=lambda: world.port)`, `app = create_app(core, run_loop=False)`, `TestClient(app)`; `await core.connect_once()` (via `asyncio.run` of async test); prefill LOAD-containers in `world.yard` (zoals `run_once`); POST orders met `TestClient` direct (`client.post("/workorders", json=[...])`); lus: `t += 60; world.run(until=t); core.step(); if client.get("/status").json()["open"] == 0: break`; `k = client.get("/kpis").json()`; assert gelijk aan `tests/e2e/fixtures/plan_a_baseline_seed1.json` op dezelfde manier als `test_plan_a_equivalence.py` (zelfde `NEW_DEFINITION`-uitzondering; vergelijk alleen sleutels uit de fixture).
- [ ] **Step 2: Run** → FAIL.  **Step 3: Implementeer.**  **Step 4: Run** `uv run pytest tests/runner tests/e2e/test_service_equivalence.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(runner): --port http against the ECS service; service loop reproduces in-process KPIs`

### Task 6: e2e over OPC UA + service, README, volledige suite

**Files:**
- Modify: `tests/e2e/opcua_procs.py` (nieuw `ecs_service(gateway_endpoint, *, log_dir) -> contextmanager` dat `python -m terminal_ecs_service.serve --gateway … --layout … --host 127.0.0.1 --port <vrij>` start en op `GET /health` 200 wacht, max. 60 s), `README.md`
- Create: `tests/e2e/test_service_small.py` (marker `e2e_opcua`)

- [ ] **Step 1: Test**: zoals `test_opcua_small.py` maar met `sim_and_gateway` + `ecs_service` en `run_once_http(..., ecs=url, gateway=endpoint)`; asserts: returncodes `(0, 0)` en ecs-service exit 0 na SIGTERM, `containers_done == 20`, `containers_failed == 0`, `timed_out == 0`, wall < 90 s.
- [ ] **Step 2: Run** `uv run pytest tests/e2e/test_service_small.py -m e2e_opcua -q` → PASS (timeout ≥ 600 s).
- [ ] **Step 3: Run** `uv run pytest -q` (default) → alles PASS; `uv run pytest -m e2e_opcua -q` → 4 PASS. Noteer tellingen.
- [ ] **Step 4: README**: sectie "ECS als service": drie CLI's (`terminal_ecs_service.serve`, `terminal_tos_stub.submit`, runner `--port http`), API-tabel (spec §3), WS-berichtsoorten (spec §5), opmerking dat sim-besturing bij de runner blijft.
- [ ] **Step 5: Commit** `test/docs: e2e sim+gateway+ECS service over HTTP; README section on the ECS service`
