# Plan B2d — Verbindingsverlies, storingen en operator-vrijgave (ontwerp)

Datum: 2026-11-10. Bouwt op B1, B3-stap 1 en B2a. Gebruikersbeslissing (10-11-2026): verbindingsverlies is géén storing — de ECS laat de job doorlopen en toont een melding; storingen blijven staan tot de verbinding hersteld is én een operator ze handmatig oplost; dat geldt voor alle storingen.

## 1. Doel en scope

Tot nu toe behandelt de ECS verbindingsverlies als FAULT (job herplannen) en herstelt een apparaat zichzelf zodra het IDLE meldt. B2d vervangt dat door:

- een expliciete toestand `OFFLINE` voor "geen verbinding", zonder herplanning;
- operator-vrijgave na elke FAULT (`release`), met een instelbare automatische vrijgave voor onbemande runs;
- expliciete afhandeling van jobs waarvan de uitkomst tijdens een onderbreking verloren ging (`resolve_job`);
- alarmen (`GET /alarms`, WS `alarm`) als zichtbare lijst van wat de operator moet doen.

**Buiten scope**: viewer-UI voor alarmen (B2b toont ze), persistentie van alarmen over een service-herstart, authenticatie, OPC UA-beveiliging.

De ECS-dispatcher wordt bewust gewijzigd; de hash-fixture `tests/fixtures/ecs_sources.sha256` wordt na afloop opnieuw gebaseerd. De Plan A-baseline (geen storingen) blijft exact gelijk.

## 2. Model (`terminal_model`)

- `State` krijgt `OFFLINE = "OFFLINE"`.
- Redenen op `STATE_CHANGED` (veld `reason`): `LINK_LOST`, `LINK_RESTORED` (ECS↔gateway, gezet door `ReconnectingPort`), `UPSTREAM_DISCONNECTED`, `UPSTREAM_RECONNECTED` (gateway↔apparaat, bestaand). Constanten in `terminal_model.port` als `LinkReason` (StrEnum).
- `EquipmentPort`-protocol ongewijzigd.

## 3. Gateway (`terminal_gateway.mirror`)

| Moment | Was (B1/B3) | Wordt |
|---|---|---|
| upstream-verbinding weg | `State = FAULT`, `StateChanged(FAULT, UPSTREAM_DISCONNECTED)` | `State = OFFLINE`, `StateChanged(OFFLINE, UPSTREAM_DISCONNECTED)` |
| upstream terug | recovery-event alleen als upstream ≠ FAULT | `StateChanged(<verse upstream-toestand>, UPSTREAM_RECONNECTED)` altijd, ook bij FAULT |

Snapshot-variabelen worden bij herverbinding overgenomen zoals nu. OPC UA `State`-variabele kan de string `OFFLINE` dragen (String-type, geen schemawijziging).

## 4. ECS-zijde port (`terminal_ecs_service.port.ReconnectingPort`)

- `unbind()` (verbinding met gateway weg): voor elk bekend apparaat `StateChanged(OFFLINE, LINK_LOST)` aan de abonnees; snapshot-cache zet `state = OFFLINE`.
- `bind()` na eerder verlies: voor elk apparaat `StateChanged(<verse toestand>, LINK_RESTORED)` — altijd, ook als de toestand gelijk is aan vóór het verlies; cache eerst verversen, dan events leveren. De vergelijkings-/reconciliatielogica uit B2a (`LINK_RESTORED` alleen bij verschil, job-id-warning) vervalt: de Dispatcher doet de reconciliatie nu zelf (§5).
- Eerste `bind()` (opstart): geen events.

## 5. Dispatcher (`terminal_ecs.dispatcher`)

Nieuwe constructor-parameter `auto_release: bool = False`. Nieuwe interne verzamelingen: `_offline: set[str]`, `_needs_release: set[str]`, `_unknown: dict[str, str]` (job_id → equipment_id), `_offline_job: dict[str, str]` (equipment_id → job_id die liep toen het apparaat OFFLINE ging).

### 5.1 OFFLINE

- `STATE_CHANGED → OFFLINE`: apparaat in `_offline`; lopende job (uit `_job_eq`) onthouden in `_offline_job`; **geen** herplanning, order blijft in zijn stage; apparaat krijgt geen nieuwe jobs (`_available` is False voor `_offline`); watchdog: voor offline AGV's wordt `_last_progress` bij elke tick op `now` gezet (tijd bevriest).
- `STATE_CHANGED` met reden `LINK_RESTORED` of `UPSTREAM_RECONNECTED` voor een apparaat in `_offline`:
  - `BUSY`: apparaat uit `_offline`; als `_offline_job` nog in `_job_eq` staat loopt de job gewoon door.
  - `IDLE`: apparaat uit `_offline`; was er een `_offline_job` die nog in `_job_eq` staat en is er sindsdien geen `JOB_COMPLETED`/`JOB_FAILED` voor gezien, dan `_unknown[job_id] = eid` en `eid` in `_needs_release` (bij `auto_release=True`: direct `resolve_job(job_id, "completed")` en geen `needs_release` — een job die al liep toen de verbinding wegviel kan alleen in `JOB_COMPLETED` of in een FAULT eindigen; faalt het apparaat, dan meldt het FAULT en niet IDLE). Zonder lopende job: apparaat is weer inzetbaar (tenzij in `_needs_release`).
  - `FAULT`: apparaat uit `_offline`, verder als §5.2.
  - `OFFLINE` (gateway meldt nog steeds offline): blijft in `_offline`.
- Een `JOB_COMPLETED`/`JOB_FAILED` dat tijdens OFFLINE tóch binnenkomt (kan niet via de port, wel in tests) wordt normaal verwerkt en wist `_offline_job`.

### 5.2 FAULT en vrijgave

- `STATE_CHANGED → FAULT`: zoals nu herplannen (`_replan(..., "FAULT")`) en apparaat in `_excluded`; nieuw: ook in `_needs_release`.
- `STATE_CHANGED → IDLE`: heft `_excluded` **alleen** op als het apparaat niet in `_needs_release` zit — of als `auto_release=True` (dan ook `_needs_release` wissen). Dit is de enige gedragswijziging voor runs zonder operator: met `auto_release=True` is het gedrag identiek aan nu.
- `release(equipment_id) -> None`: `KeyError` bij onbekend apparaat; `ReleaseRefused(reason)` als het apparaat nu `FAULT` of `OFFLINE` meldt (`port.snapshot`) of als er nog een `_unknown`-job voor dit apparaat openstaat (eerst `resolve_job`); anders uit `_needs_release` en `_excluded`, daarna `tick()`.

### 5.3 Onbekende uitkomst

- `resolve_job(job_id, outcome: Literal["completed","failed"]) -> None`: `KeyError` als `job_id` niet in `_unknown`. `completed` → zelfde pad als `_on_completed` met een gesynthetiseerd `JOB_COMPLETED` (sim_time = `now`); `failed` → `_replan(rec, eid, "UNKNOWN_OUTCOME")`. Daarna `_unknown.pop(job_id)`; het apparaat blijft in `_needs_release` tot `release`.

### 5.4 Status en alarmen

`status()` krijgt `offline` (aantal), `needs_release` (aantal), `unknown_outcomes` (aantal). Nieuw `alarms() -> list[Alarm]` met `Alarm(kind: AlarmKind, equipment_id: str, job_id: str | None, since: float, detail: str)`; `AlarmKind` = `LINK_LOST` (per offline apparaat), `NEEDS_RELEASE`, `UNKNOWN_OUTCOME`. `since` = sim-tijd van ontstaan. Alarmen zijn afgeleid van de verzamelingen (geen aparte opslag); `_alarm_since: dict[tuple[AlarmKind, str], float]` bewaart het ontstaansmoment.

## 6. Service-API (`terminal_ecs_service`)

| Endpoint | Antwoord |
|---|---|
| `GET /alarms` | `[{"kind","equipment_id","job_id","since_sim_time","detail"}]`, gesorteerd op `since_sim_time` |
| `POST /equipment/{id}/release` | 200 `{"released": id}`; 404 onbekend; 409 `{"code":"RELEASE_REFUSED","message":…}`; 503 zonder verbinding |
| `POST /jobs/{job_id}/resolve` body `{"outcome":"completed"\|"failed"}` | 200 `{"job_id","outcome"}`; 404 onbekend/geen onbekende uitkomst; 422 ongeldige outcome; 503 |

WS-bericht `{"type":"alarm","action":"raised"\|"cleared","kind",…alarmvelden,"sim_time"}` na elke `step()`/operator-aanroep waarin de alarmlijst veranderde (diff op `(kind, equipment_id, job_id)`). `status`-bericht krijgt de drie nieuwe tellingen mee (via `Dispatcher.status()`).

CLI: `terminal_ecs_service.serve --auto-release` (standaard uit); `terminal_runner.run --no-auto-release` (standaard aan: onbemande runs gedragen zich als nu). `EcsCore(layout, *, connect, broadcaster=None, auto_release=False)`.

## 7. Foutafhandeling

| Situatie | Gedrag |
|---|---|
| `release` op apparaat dat FAULT/OFFLINE meldt | 409, apparaat blijft `needs_release` |
| `release` met openstaande onbekende uitkomst | 409 `"resolve job <id> first"` |
| `resolve` met ongeldige outcome | 422 |
| OFFLINE-event voor onbekend apparaat | genegeerd met warning |
| Reconnect met andere vloot | bestaande `_check_fleet`-melding (runner); service logt ERROR en blijft op de oude vloot |
| `auto_release=True` en onbekende uitkomst | direct `completed` → order gaat verder, geen alarm |

## 8. Tests

- `tests/ecs`: OFFLINE herplant niet en pauzeert de watchdog (600 s offline → geen cancel); terugkeer BUSY/IDLE-met-job/IDLE-zonder-job/FAULT/OFFLINE; FAULT → `needs_release`, IDLE heft niet op; `release` geweigerd bij FAULT en bij open onbekende uitkomst, toegestaan bij IDLE; `resolve_job` completed en failed; `auto_release=True`: alle bestaande dispatcher-tests ongewijzigd groen (fixture draait met `auto_release=True`), onbekende uitkomst → completed; `alarms()`-inhoud en `since`.
- `tests/gateway`: OFFLINE i.p.v. FAULT bij verlies; recovery-event ook bij upstream FAULT.
- `tests/ecs_service`: port LINK_LOST voor alle apparaten bij `unbind`, LINK_RESTORED voor alle bij `bind`; API alarms/release/resolve incl. 404/409/422/503; WS `alarm` raised/cleared; `EcsCore` met FakePort: offline → terug IDLE met job → alarm UNKNOWN_OUTCOME → resolve → release → apparaat weer toegewezen.
- `tests/e2e`: Plan A-baseline exact (geen storingen); `faults.yaml` in-process met `auto_release=True` → tellingen gelijk aan vóór B2d (nieuwe referentie-fixture voor faults seed 1 vastleggen vóór de wijziging, in Task 1); `e2e_opcua`: proxy-blip sim↔gateway tijdens een run (service `--auto-release`) → run voltooit zonder `stuck`; proxy-blip ECS↔gateway → idem.
- Nieuwe ECS-hash-fixture als laatste stap; architectuurtest ongewijzigd.

## 9. Documentatie

README: sectie "Storingen, verbindingsverlies en vrijgave" (toestanden, alarmen, de drie endpoints, `--auto-release`). B1-spec §4 verwijst naar dit document voor OFFLINE.

## 10. Pad verder

B2b toont alarmen en `OFFLINE` in de viewer en biedt release/resolve-knoppen. B2c zet `--auto-release` uit op de NAS pas wanneer de viewer er is; de duurtest draait eerst met auto-release.
