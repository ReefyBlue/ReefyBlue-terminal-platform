# Plan B2d — Verbindingsverlies, storingen en operator-vrijgave: implementatieplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verbindingsverlies wordt een eigen toestand `OFFLINE` zonder herplanning; elke FAULT vereist operator-vrijgave (`release`), verloren jobuitkomsten worden expliciet opgelost (`resolve_job`), en de ECS publiceert alarmen via API en websocket — met `auto_release` voor onbemande runs.

**Architecture:** `State.OFFLINE` en `LinkReason` in het model; gateway en `ReconnectingPort` zenden OFFLINE/herstel-events; de Dispatcher krijgt `_offline`, `_needs_release`, `_unknown`, `release()`, `resolve_job()`, `alarms()` en `auto_release`; de service legt `GET /alarms`, `POST /equipment/{id}/release`, `POST /jobs/{id}/resolve` en WS `alarm` erop. ECS-hash wordt opnieuw gebaseerd; een faults-referentiefixture bewijst dat `auto_release=True` het oude gedrag behoudt.

**Tech Stack:** Python 3.13, FastAPI, asyncua 2.1.0, salabim, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-10-plan-b2d-link-loss-design.md`

## Global Constraints

- Plan A-baseline (`tests/e2e/fixtures/plan_a_baseline_seed1.json`, geen storingen) blijft exact gelijk via `run_once` (runner standaard `auto_release=True`).
- Faults-referentie: `tests/e2e/fixtures/faults_pre_b2d_seed1.json` wordt in Task 1 vastgelegd met de code vóór de Dispatcher-wijziging; met `auto_release=True` moeten `containers_done`, `containers_failed`, `replans`, `faults`, `moves_per_hour_total` daarna exact gelijk blijven.
- `tests/fixtures/ecs_sources.sha256` wordt pas in Task 7 opnieuw gebaseerd; tot dan faalt `test_architecture.py::test_ecs_sources_unchanged` bewust (documenteer dit in elke taakrapportage; voer de architectuurtest met `-k "not ecs_sources"` uit).
- Reden-strings exact: `LINK_LOST`, `LINK_RESTORED`, `UPSTREAM_DISCONNECTED`, `UPSTREAM_RECONNECTED`; alarmsoorten exact `LINK_LOST`, `NEEDS_RELEASE`, `UNKNOWN_OUTCOME`; API-antwoorden exact spec §6; HTTP-codes 200/404/409/422/503 zoals spec §6/§7.
- Pakketregels ongewijzigd (ecs→model; gateway→model+opcua; ecs_service→model+ecs+opcua+kpi+tos_stub; runner→alles).
- Defaults: `Dispatcher(auto_release=False)`, `EcsCore(auto_release=False)`, `serve --auto-release` uit, runner `--no-auto-release` uit (dus runner auto_release=True).
- Tests: `uv run pytest <pad> -q`; salabim `Environment(trace=False, yieldless=False)`.
- Commit per taak; trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` + `Claude-Session: https://claude.ai/code/session_011Q6oL3tzJGQycNuMVuzTtk`.

## Review Focus

1. Apparaat gaat OFFLINE terwijl een AGV met box onderweg is, komt na 20 min sim-tijd terug als BUSY: job loopt door, watchdog cancelt niet, geen replan — Task 3 `test_offline_busy_return_keeps_job_without_watchdog`.
2. OFFLINE → terug IDLE terwijl de job liep → `UNKNOWN_OUTCOME`; `resolve completed` laat de order op de juiste volgende stage verdergaan (TO_YARD na DISCHARGING, enz.) — Task 3 `test_resolve_completed_advances_stage`.
3. FAULT → IDLE zonder release: apparaat krijgt géén job; na `release` wel — Task 3 `test_fault_requires_release_before_reuse`.
4. Hele gateway-verbinding weg: álle apparaten OFFLINE, lopende jobs blijven; terug: `LINK_RESTORED` voor alle, orders gaan door — Task 5 `test_core_link_blip_keeps_orders_and_resumes`.
5. `release` terwijl het apparaat FAULT meldt → 409 en blijft `needs_release`; daarna IDLE + release → 200 — Task 6 `test_release_409_while_fault_then_200`.

---

### Task 1: Model + faults-referentiefixture (vóór de Dispatcher-wijziging)

**Files:**
- Modify: `packages/model/src/terminal_model/types.py` (`State.OFFLINE`), `packages/model/src/terminal_model/port.py` (`class LinkReason(StrEnum)`: `LINK_LOST`, `LINK_RESTORED`, `UPSTREAM_DISCONNECTED`, `UPSTREAM_RECONNECTED`)
- Create: `tests/e2e/fixtures/faults_pre_b2d_seed1.json` (uitvoer van `run_once(load_scenario("scenarios/faults.yaml"), layout, 1)` met de huidige code), `tests/e2e/test_faults_equivalence.py` (marker `e2e`): vergelijkt `run_once` faults seed 1 met de fixture op de vijf sleutels uit Global Constraints
- Test: `tests/model/test_types.py` (bestaand of nieuw): `State("OFFLINE") is State.OFFLINE`; `LinkReason.LINK_LOST == "LINK_LOST"`

- [ ] **Step 1:** Schrijf `tests/e2e/test_faults_equivalence.py` en genereer de fixture met een eenmalig script (`uv run python -c ...`) **vóór** enige Dispatcher-wijziging; voeg de modeltests toe.
- [ ] **Step 2: Run** `uv run pytest tests/model tests/e2e/test_faults_equivalence.py -q` → modeltests FAIL, equivalentie PASS (zelfde code).
- [ ] **Step 3:** Implementeer `State.OFFLINE` en `LinkReason`.
- [ ] **Step 4: Run** → PASS; `uv run pytest tests/test_architecture.py -q` → PASS (model gewijzigd, ecs niet).
- [ ] **Step 5: Commit** `feat(model): State.OFFLINE and LinkReason; faults reference fixture before B2d`

### Task 2: Gateway zendt OFFLINE en altijd een herstel-event

**Files:**
- Modify: `packages/gateway/src/terminal_gateway/mirror.py` (`_on_lost`: `State = OFFLINE`, event `OFFLINE` met `LinkReason.UPSTREAM_DISCONNECTED`; `_announce_recovery`: altijd `StateChanged(<upstream state>, UPSTREAM_RECONNECTED)`, ook bij FAULT; onbekende state → warning, geen event; hernoem `FAULT_REASON`/`RECOVERY_REASON` naar de `LinkReason`-waarden; docstrings)
- Test: `tests/gateway/test_gateway.py`

**Interfaces:**
- Produces: gateway-`State` kan `"OFFLINE"` zijn; recovery-event ook bij FAULT.

- [ ] **Step 1:** Pas tests aan: `test_server_drop_marks_fault` → `test_server_drop_marks_offline` (State `OFFLINE`, event `new_state OFFLINE`, reason `UPSTREAM_DISCONNECTED`); `test_blip_on_faulted_upstream_sends_no_recovery` → `test_blip_on_faulted_upstream_reports_fault` (recovery-event met `new_state FAULT`); overige blip-tests: verwacht OFFLINE i.p.v. FAULT bij verlies.
- [ ] **Step 2: Run** `uv run pytest tests/gateway -q` → FAIL.  **Step 3:** Implementeer.  **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(gateway): OFFLINE on upstream loss; recovery event also when upstream is FAULT`

### Task 3: Dispatcher — OFFLINE, vrijgave, onbekende uitkomst, alarmen

**Files:**
- Modify: `packages/ecs/src/terminal_ecs/dispatcher.py`, `packages/ecs/src/terminal_ecs/orders.py` (indien `Alarm`/`AlarmKind` daar passen; anders nieuw `packages/ecs/src/terminal_ecs/alarms.py`)
- Test: nieuw `tests/ecs/test_dispatcher_offline.py`, `tests/ecs/test_dispatcher_release.py`; bestaande dispatcher-tests krijgen `auto_release=True` via hun fixture (één plek: `tests/ecs/conftest.py` of de bestaande constructor-helper)

**Interfaces:**
- Produces: `Dispatcher(port, slot_provider, *, lookahead_per_sts=2, watchdog_s=600.0, auto_release=False)`; `class ReleaseRefused(Exception)` met `.reason: str`; `release(equipment_id: str) -> None` (`KeyError`/`ReleaseRefused`); `resolve_job(job_id: str, outcome: Literal["completed","failed"]) -> None` (`KeyError` als niet in `_unknown`; `ValueError` bij andere outcome); `alarms() -> list[Alarm]`; `@dataclass(frozen=True) Alarm(kind: AlarmKind, equipment_id: str, job_id: str | None, since: float, detail: str)`; `AlarmKind(StrEnum)`: `LINK_LOST`, `NEEDS_RELEASE`, `UNKNOWN_OUTCOME`; `status()` + `offline`, `needs_release`, `unknown_outcomes` (ints). Gedrag exact spec §5.1–§5.4.
- `_on_state` herkent OFFLINE-terugkeer aan `ev.reason in (LinkReason.LINK_RESTORED, LinkReason.UPSTREAM_RECONNECTED)` en `eid in self._offline`.

- [ ] **Step 1: Tests** (FakePort; helpers uit bestaande dispatcher-tests hergebruiken):

```python
# test_dispatcher_offline.py
def test_offline_does_not_replan_and_blocks_dispatch(): AGV met job → STATE_CHANGED OFFLINE → status()["replans"] == 0, job nog in dispatcher.jobs, status()["offline"] == 1; nieuwe orders gaan niet naar de offline AGV
def test_offline_busy_return_keeps_job_without_watchdog(): OFFLINE; fake.advance(1200); tick ×N → geen cancel_job; STATE_CHANGED BUSY reason LINK_RESTORED → offline == 0, job loopt; complete → order gaat door
def test_offline_idle_return_with_job_is_unknown_outcome(): OFFLINE met job; terug IDLE (LINK_RESTORED) → status()["unknown_outcomes"] == 1, needs_release == 1; alarms() bevat UNKNOWN_OUTCOME(job_id) en NEEDS_RELEASE(eid)
def test_offline_idle_return_without_job_is_available(): geen job → apparaat krijgt weer jobs
def test_offline_fault_return_replans_and_needs_release()
def test_offline_event_for_unknown_equipment_ignored(caplog)
def test_resolve_completed_advances_stage(): DISCHARGING-job onbekend → resolve completed → order in TO_YARD (via volgende assign) ; resolve failed → replan
def test_resolve_unknown_job_keyerror(); def test_resolve_bad_outcome_valueerror()
def test_auto_release_resolves_unknown_as_failed(): Dispatcher(auto_release=True) → replans == 1, geen alarm

# test_dispatcher_release.py
def test_fault_requires_release_before_reuse(): FAULT → IDLE → geen job toegewezen; release → job toegewezen
def test_release_refused_while_fault_or_offline(): ReleaseRefused.reason bevat "FAULT" / "OFFLINE"
def test_release_refused_with_open_unknown_outcome(): reason bevat job_id
def test_release_unknown_equipment_keyerror()
def test_auto_release_idle_clears_like_before(): Dispatcher(auto_release=True): FAULT → IDLE → job toegewezen
def test_alarm_since_is_creation_time(): fake.advance(100) vóór FAULT → alarms()[0].since == 100.0; blijft 100.0 na verder advance
```

- [ ] **Step 2: Run** `uv run pytest tests/ecs -q` → FAIL.
- [ ] **Step 3: Implementeer** per spec §5; zet bestaande dispatcher-testfixtures op `auto_release=True`.
- [ ] **Step 4:** Zet in `packages/runner/src/terminal_runner/run.py` bij `run_once` en `run_once_opcua` `Dispatcher(..., auto_release=True)` (één regel per functie; de CLI-vlag komt in Task 6). **Run** `uv run pytest tests/ecs tests/e2e/test_plan_a_equivalence.py tests/e2e/test_faults_equivalence.py -q` → PASS; `uv run pytest tests/test_architecture.py -q -k "not ecs_sources"` → PASS (de hash-test faalt bewust tot Task 7; controleer de exacte testnaam in het bestand).
- [ ] **Step 5: Commit** `feat(ecs): OFFLINE handling, operator release, unknown-outcome resolution and alarms`

### Task 4: ReconnectingPort — LINK_LOST/LINK_RESTORED voor alle apparaten

**Files:**
- Modify: `packages/ecs_service/src/terminal_ecs_service/port.py` (`unbind`: OFFLINE-event per apparaat met `LinkReason.LINK_LOST`, cache state OFFLINE; `bind` na verlies: `StateChanged(<vers>, LINK_RESTORED)` voor álle apparaten na cache-verversing; verwijder de B2a-vergelijkingslogica en de job-id-warning), `core.py` (`on_link_lost` roept `unbind` aan zoals nu; niets anders)
- Test: `tests/ecs_service/test_reconnecting_port.py`

- [ ] **Step 1: Tests**: `test_unbind_emits_offline_for_all_equipment` (reason LINK_LOST, cache state OFFLINE, `snapshot().state is OFFLINE`); `test_rebind_emits_link_restored_for_all` (ook ongewijzigde toestand; `new_state` = verse toestand); `test_first_bind_emits_nothing`; verwijder/vervang de B2a-reconcile-tests.
- [ ] **Step 2: Run** → FAIL.  **Step 3:** Implementeer.  **Step 4: Run** `uv run pytest tests/ecs_service -q` → PASS (core-tests die op LINK_RESTORED-bij-verschil rekenden aanpassen).
- [ ] **Step 5: Commit** `feat(ecs_service): LINK_LOST/LINK_RESTORED for every device on unbind/bind`

### Task 5: EcsCore — auto_release, alarm-diff, WS `alarm`

**Files:**
- Modify: `packages/ecs_service/src/terminal_ecs_service/core.py` (`EcsCore(..., auto_release=False)` → `Dispatcher(..., auto_release=auto_release)`; `alarms() -> list[dict]` (spec §6-vorm, sleutel `since_sim_time`); `publish_alarms()` na elke `step()`-tick, na `submit`, en na `release`/`resolve` (diff op `(kind, equipment_id, job_id)` → `{"type":"alarm","action":"raised"|"cleared",...,"sim_time"}`); `release(equipment_id)`, `resolve_job(job_id, outcome)` als dunne wrappers die daarna `publish_alarms()` en `publish_status()` doen)
- Test: `tests/ecs_service/test_ecs_core.py`

- [ ] **Step 1: Tests**: `test_core_link_blip_keeps_orders_and_resumes` (FakePort, order toegewezen, `on_link_lost()` → status offline == n, alarms LINK_LOST per apparaat, broadcaster `alarm raised`; `connect_once()` met nieuwe FakePort in dezelfde staat (BUSY, zelfde job) → offline 0, alarms leeg, `alarm cleared`, order gaat door bij complete); `test_core_release_and_resolve_publish` ; `test_core_auto_release_passthrough` (`EcsCore(auto_release=True)` → `dispatcher.auto_release is True`).
- [ ] **Step 2: Run** → FAIL.  **Step 3:** Implementeer.  **Step 4: Run** `uv run pytest tests/ecs_service -q` → PASS.
- [ ] **Step 5: Commit** `feat(ecs_service): auto_release option, alarm list and alarm WS messages`

### Task 6: API-endpoints, CLI-vlaggen, runner

**Files:**
- Modify: `packages/ecs_service/src/terminal_ecs_service/app.py` (+ `schemas.py`: `AlarmOut`, `ReleaseOut`, `ResolveIn(outcome: Literal["completed","failed"])`, `ResolveOut`, `RefusedOut(code, message)`), `serve.py` (`--auto-release`), `packages/runner/src/terminal_runner/run.py` (`run_once`/`run_once_opcua`: `Dispatcher(..., auto_release=True)`; CLI `--no-auto-release`; http-modus: geen wijziging — de service bepaalt), `packages/tos_stub/src/terminal_tos_stub/client.py` (`alarms()`, `release(eid)`, `resolve_job(job_id, outcome)`)
- Test: `tests/ecs_service/test_app.py`, `tests/runner/test_run.py`, `tests/tos_stub/test_client.py`

- [ ] **Step 1: Tests**: `test_alarms_endpoint_lists_sorted`; `test_release_409_while_fault_then_200` (FakePort FAULT → 409 `RELEASE_REFUSED`, alarms bevat NEEDS_RELEASE; `set_state IDLE` → 200 `{"released": id}`, alarms leeg); `test_release_404_unknown`; `test_resolve_200_completed_and_failed`; `test_resolve_404_and_422`; `test_endpoints_503_without_connection`; `test_ws_alarm_messages`; `test_serve_auto_release_flag` (monkeypatch `uvicorn.run`; `--auto-release` → `core.dispatcher`-vlag na connect… of eenvoudiger: `EcsCore` krijgt `auto_release` — assert op het `EcsCore`-attribuut); runner: `run_once` gebruikt `auto_release=True` (bestaande equivalentietests blijven groen); `--no-auto-release` wordt doorgegeven (test met gepatchte `Dispatcher`); client: drie nieuwe methoden met MockTransport.
- [ ] **Step 2: Run** → FAIL.  **Step 3:** Implementeer.  **Step 4: Run** `uv run pytest tests/ecs_service tests/runner tests/tos_stub -q` → PASS.
- [ ] **Step 5: Commit** `feat(ecs_service,runner): alarms/release/resolve endpoints, --auto-release / --no-auto-release`

### Task 7: e2e-blips, hash opnieuw baseren, documentatie, volledige suite

**Files:**
- Create: `tests/e2e/test_blip_during_run.py` (marker `e2e_opcua`): (a) sim↔gateway: `sim_and_gateway` met tcp-proxy tussen gateway en één STS-server (hergebruik `tests/gateway/tcp_proxy.py`-aanpak via `opcua_procs` — voeg daar een `proxied_sts` optie toe die de gateway via `--upstream-host` op een proxy-poort laat verbinden voor STS-01), ECS-service `--auto-release`, runner `--port http`; tijdens de run na ~10 s wandklok `proxy.cut()`, 3 s later `restore()`; assert run voltooit, `containers_stuck == 0`, `containers_failed == 0`; (b) ECS↔gateway: proxy tussen ECS-service en gateway, idem.
- Modify: `tests/fixtures/ecs_sources.sha256` (opnieuw genereren met het bestaande script/commando uit `tests/test_architecture.py`), `README.md` (sectie "Storingen, verbindingsverlies en vrijgave"), `docs/superpowers/specs/2026-11-10-plan-b1-opcua-design.md` §4 (één regel: OFFLINE zie B2d-spec)

- [ ] **Step 1:** Schrijf en run de blip-tests (`-m e2e_opcua`, timeout ≥ 900 s) → PASS. Faalt (a) of (b) door productgedrag: rapporteer BLOCKED met logs; fix alleen testinfrastructuur.
- [ ] **Step 2:** Herbaseer de ECS-hash; `uv run pytest tests/test_architecture.py -q` → PASS.
- [ ] **Step 3: Run** `uv run pytest -q` → alles PASS; `uv run pytest -m e2e_opcua -q` → alle PASS (bestaande 4 + 2 nieuwe). Noteer tellingen.
- [ ] **Step 4:** README + B1-spec-regel.
- [ ] **Step 5: Commit** `test/docs: link blips during OPC UA runs; ECS hash re-baselined for B2d; README on faults, link loss and release`
