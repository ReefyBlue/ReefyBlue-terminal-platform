# Plan B2b — Webviewer voor de ECS-service (ontwerp)

Datum: 2026-11-11 · Status: ontwerp (vastgesteld onder de staande toestemming van de gebruiker: "alle stappen nemen totdat het bruikbaar is"; beslissingen staan in §9)

## 1. Doel

Een operator kan in de browser live zien wat de ECS-service doet — kade, STS-kranen, AGV's, stackblokken met ASC — en kan de twee handmatige acties uit B2d uitvoeren: een apparaat **vrijgeven** en een job met onbekende uitkomst **afhandelen**. Zonder viewer draait de NAS-run (B2c) alleen met `--auto-release`; mét viewer kan de gebruiker de B2d-regel "storingen blijven staan tot handmatig opgelost" daadwerkelijk toepassen.

Succescriteria:
1. `uv run terminal-ecs-service` (B2a) serveert op `/ui/` een pagina die binnen 2 s na laden de plattegrond toont en daarna live meebeweegt.
2. Een AGV die OFFLINE/FAULT gaat kleurt om binnen één poll-interval; het alarm staat in het alarmpaneel met een werkende knop **Vrijgeven** / **Afhandelen (completed|failed)**.
3. Geen build-stap, geen Node, geen externe CDN: alles komt uit de wheel en werkt offline op de QNAP.
4. De bestaande API blijft ongewijzigd (alleen toevoegingen); alle B2a/B2d-tests blijven groen.

Buiten scope (geparkeerd): workorders invoeren via de UI (TOS-stub doet dat), historische grafieken, meertaligheid, authenticatie (NAS staat achter het thuisnetwerk; B2c noteert dit als risico), mobiele lay-out.

## 2. Referenties en stijl

Vastgelegd in `docs/superpowers/references/b2b-viewer/README.md`. Samengevat:
- **Bovenaanzicht**, kade bovenaan (y=0), stack onderaan; schaal uit `Layout` (`grid_cell_m`, `grid_cols`, `grid_rows`).
- **STS** = verticale blauwe balk over kade + handover-lane met id; lane gearceerd als `handover_lane_occupied`.
- **AGV** = rechthoek met id; gevuld blokje als `loaded_container_id` niet leeg is; dunne lijn voor de resterende `route`.
- **Blok** = omtrek met blokletter, TP-cel (gearceerd als `ws_tp_occupied`) en de ASC als balk op `gantry_position_m`.
- **Toestandskleuren**: IDLE grijs-groen `#5b8c5a`, BUSY groen `#2e7d32`, FAULT rood `#c62828`, OFFLINE paars-grijs `#6d5f8a` met streeprand, MAINTENANCE oranje `#ef6c00`. Lijntekening op wit; palet blauw/groen/grijs zoals de referentie.
- **Rechterpaneel**: koppeling (verbonden/verbroken), KPI's, alarmen (oudste boven) met actieknoppen, laatste 50 events.
- **Legenda** onderaan het paneel: de proces-flow berth → internal path → yard als drie labels met kleur, geen aparte tekening.

## 3. Architectuur

```
browser ── GET /ui/            ── static index.html, viewer.js, viewer.css   (nieuw: terminal_viewer)
        ── GET /layout         ── geometrie uit Layout                         (nieuw: ecs_service)
        ── GET /equipment      ── poll 1 Hz: posities, toestand, extra        (bestaand)
        ── GET /alarms, /status, /kpis ── initiële vulling                      (bestaand)
        ── WS  /events         ── status/equipment/job/link/alarm               (bestaand)
        ── POST /equipment/{id}/release, POST /jobs/{id}/resolve                (bestaand)
```

- **Nieuw package `terminal_viewer`** (`packages/viewer`): alleen statische bestanden (`static/index.html`, `viewer.js`, `viewer.css`) + `terminal_viewer.static_dir() -> Path`. Geen Python-logica, geen dependencies. Hatch neemt `static/` op in de wheel.
- **ECS-service** mount de map op `/ui` via `StaticFiles(html=True)` als `terminal_viewer` importeerbaar is (optional dependency `viewer`); anders geen mount en één logregel. `GET /` redirect naar `/ui/` wanneer gemount.
- **`GET /layout`** (ecs_service): JSON met `quay_length_m, grid_cell_m, grid_cols, grid_rows, sts[{equipment_id,x,y,lane_cells}], blocks[{block,asc_id,tp_ws{x,y},tp_ws_cell,bays,rows,tiers}], agv_start_cells`. Komt uit de al geladen `Layout`; geen apparaattoestand. Altijd beschikbaar, ook zonder gateway-verbinding.
- **Frontend**: vanilla ES-module, Canvas 2D voor de plattegrond, DOM voor het paneel. Geen framework. Eén `state`-object; render op `requestAnimationFrame` alleen wanneer `dirty`.

Waarom polling voor posities: de WS-stream draagt gebeurtenissen, geen posities; de dispatcher tikt per `SAMPLE_INTERVAL_S`. 1 Hz `GET /equipment` is voor 6 AGV's goedkoop en vermijdt een nieuw WS-berichttype. (Geparkeerd: een `snapshot`-WS-bericht als het aantal apparaten groeit.)

## 4. Frontend-componenten (`viewer.js`)

| Module (functie-groep) | Doet | Afhankelijk van |
|---|---|---|
| `api` | `fetch` wrappers voor layout/equipment/alarms/status/kpis/release/resolve; gooit `ApiError{status, code, message}` | fetch |
| `stream` | WS-client op `/events`; exponentiële reconnect 1→10 s; zet `link.ws=false` bij verbreking | WebSocket |
| `poll` | `GET /equipment` elke 1000 ms; bij 503 markeert `link.ecs=false`; backoff 5 s na fout | api |
| `model` | `state = {layout, equipment: Map, alarms: [], status, kpis, events: ring(50), link:{ws,ecs,connected}}`; `apply(message)` per WS-type | — |
| `map` | Canvas: `fit(layout)` (schaal/offset), `draw(state)`; tekent kade, grid (licht), STS, blokken+TP+ASC, AGV's, routes; hit-test voor klik → selectie | model |
| `panel` | DOM: koppelingsbanner, KPI-tabel, alarmlijst met knoppen, eventlog, selectiedetail (extra-velden van het aangeklikte apparaat) | model, api |
| `main` | bootstrap: layout → initiële snapshots → start stream+poll → rAF-loop | alles |

Berichten → model:
- `status` → `status`, `kpis` uit `/kpis` worden bij elke `status` opnieuw gepolled (1× per tick).
- `equipment` → event in log; `new_state` direct toepassen op `equipment[id].state` (niet wachten op poll).
- `alarm raised|cleared` → alarmlijst bijwerken (sleutel `kind+equipment_id+job_id`).
- `job` → event in log.
- `link` → `link.connected`.

## 5. Acties en foutafhandeling

- **Vrijgeven**: knop per alarm `NEEDS_RELEASE`/`LINK_LOST`-op-apparaat → `POST /equipment/{id}/release`. 200 → alarm verdwijnt via de volgende `alarm cleared`. 404/409/503 → melding in de alarmregel (`code: message`) 5 s, knop weer actief. Knop gedeactiveerd tijdens request.
- **Afhandelen**: alarm `UNKNOWN_OUTCOME` → twee knoppen `Voltooid` / `Mislukt` → `POST /jobs/{id}/resolve {"outcome":"completed"|"failed"}`. Zelfde foutweergave. Eerste klik vraagt geen bevestiging: de actie is omkeerbaar in de zin van B2d (een verkeerde `completed` levert later een DUPLICATE/UNKNOWN op, geen dataverlies). Beslissing §9.3.
- **Verbinding**: banner bovenaan paneel: groen "ECS ↔ gateway verbonden", rood "gateway verbroken sinds …" (uit `link`), grijs "viewer ↔ ECS verbroken" (WS dicht én poll faalt). Plattegrond blijft laatste stand tonen, gedimd (alpha 0.5).
- **Layout ontbreekt** (`/layout` faalt): foutmelding in canvas, retry elke 5 s.

## 6. Backend-wijzigingen (ecs_service)

1. `GET /layout` → `LayoutOut` (pydantic) uit `core.layout`. Geen 503-gate.
2. `create_app`: na de routes `if (d := _viewer_dir()) is not None: app.mount("/ui", StaticFiles(directory=d, html=True)); @app.get("/") → RedirectResponse("/ui/")`. `_viewer_dir()` importeert `terminal_viewer` lazily; `ImportError` → `None`.
3. `pyproject`: `[project.optional-dependencies] viewer = ["terminal-viewer"]`; workspace source. De runner/e2e-helper installeren met `--extra viewer` niet nodig; tests mocken niets: ze draaien met het package aanwezig (workspace).
4. README-sectie "Webviewer (B2b)".

## 7. Testen

- **Unit (pytest, Starlette TestClient)**: `GET /layout` velden en waarden voor `terminal.yaml`; `/ui/` levert 200 en `text/html`; `/` → 307 naar `/ui/`; zonder `terminal_viewer` (monkeypatch `_viewer_dir` → None) geen `/ui` (404) en `/` 404.
- **Frontend**: geen Node- of JS-engine-toolchain (geen Node op de NAS, geen tweede toolchain). `model` en `map.fit` blijven wel zuivere functies zonder DOM, zodat ze in de browsertest via `window.__viewer` te prikken zijn. Eén e2e-browsertest met **Playwright (Python)** als `e2e_ui`-marker, standaard gedeselecteerd zoals `e2e_opcua`; CI-loos project, dus de marker wordt in de SDD-taak lokaal gedraaid en in het rapport vastgelegd. De test: start sim+gateway+service (`opcua_procs`), opent `/ui/`, wacht tot canvas getekend is (`window.__viewer.ready`), checkt dat `#alarms` leeg is, knipt de link (`LinkProxy`), wacht op alarmregel `LINK_LOST`, herstelt, en checkt een `NEEDS_RELEASE`-vrijgave via de knop (fault-scenario). Playwright + chromium zijn dev-dependencies; installatie `uv run playwright install chromium` staat in README (ontwikkelmachine, niet NAS).
- **Visuele check**: `tests/ui/render_check.py` maakt met Playwright een PNG van `/ui/` na 30 s simulatie → `docs/superpowers/reports/plan-b2b/screenshot.png` (handmatig beoordeeld tegen de referenties; geen pixel-diff).
- **Architecture-test**: `terminal_viewer` mag niets importeren; `ecs_service` mag `terminal_viewer` alleen in `_viewer_dir` importeren.

## 8. Uitvoering

Subagent-driven development, één taak per rij (plan volgt via writing-plans). Branch `feat/plan-b2b-viewer` op `feat/plan-b2d-link-loss`; PR #6 met base PR #5. Volgorde: backend (`/layout`, mount) → `terminal_viewer` skelet + `map` → `panel` + acties → stream/poll + verbindingsbanner → e2e_ui + screenshot → README.

## 9. Beslissingen (zonder vragen aan de gebruiker)

1. **Viewer in de ECS-service-proces** i.p.v. aparte server: één poort, geen CORS, één container op de NAS. Later losmaken kan door `terminal_viewer` elders te mounten.
2. **Vanilla JS + Canvas 2D, geen build**: de NAS heeft geen Node; de UI is klein (één scherm). Een framework zou alleen tooling toevoegen.
3. **Geen bevestigingsdialoog** bij Vrijgeven/Afhandelen: operatoracties zijn één klik zoals in de referentie-ECS; de API valideert. Terug te draaien als het in gebruik misklikken oplevert.
4. **Posities via 1 Hz polling**, events via WS (zie §3).
5. **Playwright alleen als dev/e2e**, gemarkeerd en standaard gedeselecteerd; geen UI-test in de default suite.
6. **Nederlandse labels** in de UI (gebruiker communiceert in het Nederlands; referentie-ECS is Engels, maar de operator hier is de gebruiker zelf). Codes (`LINK_LOST` enz.) blijven Engels, zoals de API.
