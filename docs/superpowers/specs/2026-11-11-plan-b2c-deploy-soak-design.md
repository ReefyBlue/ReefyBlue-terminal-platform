# Plan B2c — Docker Compose op de NAS en de duurtest (ontwerp)

Datum: 2026-11-11 · Status: ontwerp (vastgesteld onder de staande toestemming; beslissingen in §9)

## 1. Doel

Het platform (sim → gateway → ECS-service + viewer) draait als één `docker compose`-stack op de QNAP TS-473A (Container Station, x86-64) en blijft 48–72 uur onbemand stabiel onder storingen en verbindingscuts. Een **soak-driver** houdt het terminalwerk continu aan de gang en schrijft elk uur een meetpunt; een **rapportscript** maakt daar een beoordeling van met pass/fail per criterium.

Succescriteria (de duurtest op de NAS):
1. Geen container-herstart (`restart_count == 0` voor sim, gateway, ecs, chaos, soak) over de hele duur.
2. ECS-geheugen stabiel: RSS in het laatste uur ≤ 1,2 × RSS in uur 2.
3. Doorzet stabiel: moves/uur per venster van 6 uur binnen ±20 % van het eerste venster; geen `YARD_FULL`.
4. Elke verbindingscut herstelt vanzelf: na elke cut `offline == 0` en `connected == true` binnen 120 s; alle LINK_LOST-alarmen gewist.
5. `needs_release` en `unknown_outcomes` keren terug naar 0 binnen 10 min na elke storing (auto-release aan, zie §6).
6. Viewer bereikbaar op `http://<nas>:8000/ui/` gedurende de hele run (driver pollt `/ui/` elk uur: 200).

Buiten scope: TLS/authenticatie (B3), Kubernetes, meerdere terminals, ECS-herstart midden in een run als ondersteund scenario (§7 documenteert dit als beperking), echte vendor-apparatuur.

Verifieerbaar in deze omgeving: alles behalve de echte 48–72 u-run en de image-build (geen Docker-daemon hier; `docker compose config` valideert wel). De driver en het rapport worden hier getest tegen lokale processen (`tests/e2e/opcua_procs.py`) in een korte "mini-soak" van 10 minuten. De lange run doet de gebruiker op de NAS met één commando; het rapport beoordeelt hem.

## 2. Architectuur (compose)

```
deploy/
  Dockerfile                 één image, alle workspace-packages (uv, python:3.12-slim, linux/amd64)
  docker-compose.yml         sim, gateway, chaos, ecs, soak
  .env.example               SCENARIO, SPEED_FACTOR, SOAK_HOURS, CUT_EVERY_S, CUT_FOR_S, AUTO_RELEASE
  scenarios/soak.yaml        storingen aan (als faults.yaml), discharge_share 0.5, speed_factor 1
  README.md                  NAS-stappen (Container Station: "Create application" met deze compose)
```

| service | commando | poorten | health |
|---|---|---|---|
| `sim` | `python -m terminal_sim.serve /app/deploy/scenarios/soak.yaml --layout /app/terminal.yaml --autostart` | 48400–48412 intern | TCP 48400 open |
| `gateway` | `python -m terminal_gateway.serve --config /app/terminal.yaml --upstream-host sim --port 4840` | 4840 intern | TCP 4840 |
| `chaos` | `python -m terminal_soak.chaos --listen 4840 --target gateway:4840 --every $CUT_EVERY_S --for $CUT_FOR_S` | 4840 intern | TCP 4840 |
| `ecs` | `python -m terminal_ecs_service.serve --gateway opc.tcp://chaos:4840 --layout /app/terminal.yaml --host 0.0.0.0 --port 8000 [--auto-release]` | **8000:8000** | `GET /health` 200 |
| `soak` | `python -m terminal_soak.driver --ecs http://ecs:8000 --layout /app/terminal.yaml --hours $SOAK_HOURS --out /data/soak` | – | – |

- `depends_on` met `condition: service_healthy` in de keten sim → gateway → chaos → ecs → soak.
- `restart: unless-stopped` voor alle services; `logging: json-file, max-size 20m, max-file 5`.
- Volume `soakdata:/data/soak` (meetpunten + rapport); bind-mount van `./deploy/scenarios` zodat de gebruiker scenario's kan wisselen zonder rebuild.
- JSON-logging staat al aan (`configure_json_logging`) → `docker compose logs` is parseerbaar.
- Image: multi-stage; stage 1 `uv sync --frozen --no-dev --all-packages`; stage 2 kopieert `.venv` + sources; `PYTHONUNBUFFERED=1`; geen root (`USER app`).

## 3. Nieuw package `terminal_soak` (`packages/soak`)

Afhankelijkheden: `terminal_tos_stub` (EcsClient, generator, allocator), `terminal_model`, `httpx`. Drie modules:

### 3.1 `chaos.py` — verbindingscuts
`LinkProxy` verhuist van `tests/e2e/opcua_procs.py` naar `terminal_soak.linkproxy` (de test importeert hem vandaar; gedrag ongewijzigd). `chaos.main`: start de proxy `listen → target`, en elke `--every` s (default 14400) een cut van `--for` s (default 30); `--every 0` = nooit cutten. Logt `cut`/`restore` als JSON met tijdstip. Eén route-set: 1 poort (de gateway is één server).

### 3.2 `driver.py` — werk aanleveren en meten
- Start: wacht tot `GET /health` 200 (max 120 s); leest `/status`; als `done+open+failed > 0` (ECS had al werk) gaat hij **door** (geen fresh-check zoals de runner: een soak mag een herstarte driver overleven).
- Werklus (elke 10 s wall): als `open < LOW_WATER` (default 40) → genereert een batch van `BATCH` (default 60) workorders met `generate_workorders` en **uniek-blijvende ids** (`SOAK-<batchnr>-<i>`), 50 % DISCHARGE / 50 % LOAD; LOAD-orders gebruiken alleen slots die door een eerdere DISCHARGE bezet zijn (de driver houdt de yard bij: `occupied: dict[slot, container_id]`; een LOAD neemt een bezette slot en geeft hem vrij na `JOB_COMPLETED`, een DISCHARGE neemt een vrije). Zo blijft de yard in balans en komt `YARD_FULL` niet door het ontwerp. Bij de allereerste batches (lege yard) zijn er alleen DISCHARGE-orders tot ≥ `BATCH/2` slots bezet zijn.
- Completions per container zijn nu niet opvraagbaar (`/status` telt alleen; WS-`job`-berichten dragen job-ids die de ECS toekent, niet container-ids). Daarom krijgt de ECS-service één kleine uitbreiding: `GET /containers?since=<n>` → `{next: <n'>, items: [{seq, container_id, state: open|done|failed, job_type, yard_slot}]}` uit de dispatcher-records, `seq` = volgnummer van de laatste statuswijziging, `since` = paging. De driver pollt dit elke 10 s en werkt `occupied` bij. Geen WS-afhankelijkheid in de driver: eenvoudiger en herstart-bestendig.
- Meetpunt (elk uur, en bij start/stop): JSONL-regel in `/data/soak/samples.jsonl` met `wall_time, sim_time, status{…}, kpis{…}, health{rss_mb, uptime_s, connected}, alarms_count_by_kind, ui_ok (GET /ui/ == 200), cuts_seen` (uit `/alarms`-historie: driver telt LINK_LOST raised → cleared overgangen zelf door elke 10 s `/alarms` te vergelijken; herstelduur per cut gelogd).
- Stopt na `--hours` (default 72) of SIGTERM; schrijft een laatste meetpunt.

### 3.3 `report.py` — beoordeling
`python -m terminal_soak.report /data/soak/samples.jsonl [--restarts restarts.json] > soak-report.md`: tabel met uurwaarden (moves/uur, RSS, offline, needs_release, unknown, alarmen), per criterium §1 PASS/FAIL met getallen, en een lijst van alle cuts met herstelduur. `restarts.json` komt van `deploy/collect.sh` (`docker inspect --format '{{.RestartCount}}'` per container) dat de gebruiker na de run draait; ontbreekt het, dan staat criterium 1 op "niet gemeten".

## 4. Wijzigingen aan bestaande packages

1. **ecs_service**: `GET /health` krijgt `rss_mb` (uit `resource.getrusage`) en `uptime_s`; `GET /containers?since=N` (§3.2); `known_containers` wordt begrensd: afgeronde containers (done/failed) ouder dan `KNOWN_RETENTION` (default 10 000 afgeronde records, FIFO) verlaten de set — de DUPLICATE_CONTAINER-check blijft werken voor open werk; de dispatcher-records voor afgeronde orders worden in dezelfde FIFO gesnoeid (`Dispatcher.prune_done(keep=…)`), zodat RSS over 72 u niet lineair groeit. (Dit raakt de ECS-bronhash → fixture `tests/fixtures/ecs_sources.sha256` herbaselinen; gedrag van actieve orders onveranderd, equivalentiefixtures moeten exact blijven kloppen.)
2. **ecs (dispatcher)**, geparkeerd B2d-punt "quarantaine onder auto_release": met `auto_release=True` wordt een quarantaine **wél** opgeheven zodra het apparaat IDLE meldt én `extra.loaded_container_id` leeg is (geen box aan boord → niets te bergen); met een box aan boord blijft de quarantaine staan (handmatig via viewer). Zonder auto_release ongewijzigd. Spec B2d §5.3a krijgt één zin erbij.
3. **ecs (dispatcher)**, geparkeerd B2a-punt "publish_new_jobs O(N)": nieuwe jobs komen uit een `deque` van sinds de vorige tick aangemaakte jobs i.p.v. een scan over alle records. Geparkeerd "status reads without clients": `publish_status` slaat het bouwen van het bericht over als `Broadcaster` geen clients heeft.
4. **gateway**, geparkeerd "assign-during-cut job-id reuse": `Assign` van een job-id die het upstream-apparaat al als `CurrentJobId` of `LastJobId` meldt, antwoordt **idempotent** True (de job loopt/liep al) in plaats van opnieuw uit te voeren. Test: bestaande blip-flake-reproductie als unit test op de gateway-adapter.
5. **tos_stub allocator**: ongewijzigd. De runner genereert alle orders van één run vooraf, dus `YARD_FULL` is daar alleen een scenario-fout (`n_containers` > slots); het soak-probleem lost de driver op met zijn eigen yard-administratie (§3.2).
6. **Log noise** (B2a geparkeerd): tijdens een uitval logt de ReconnectingPort het herverbind-pogingbericht met backoff (1, 2, 5, 10, dan elke 30 s) in plaats van elke 2 s.

## 5. Mini-soak (hier uitvoerbaar) — `tests/e2e/test_mini_soak.py` (marker `e2e_opcua`, extra marker `slow`)

Start sim (`--autostart`, scenario `deploy/scenarios/soak.yaml` maar met `speed_factor: 20`), gateway, chaos (`--every 120 --for 15`), ECS (`--auto-release`), en de driver in een thread met `--hours 0.17` (10 min), meetpunt elke 60 s (`--sample-every`). Daarna `report.py` op de samples; asserties: ≥ 3 cuts gezien, alle hersteld < 120 s; `needs_release == 0` en `unknown_outcomes == 0` in het laatste meetpunt; moves/uur > 0 in elk venster; geen `YARD_FULL` in het ECS-log; `rss_mb` laatste ≤ 1,5 × tweede meetpunt (korte run: ruime marge). Draait ~11 min; niet in de default suite.

## 6. Auto-release in de soak

De NAS-stack draait met `AUTO_RELEASE=1` (default in `.env.example`): zonder operator moeten storingen vanzelf vrijkomen; §4.2 maakt dat ook voor quarantaine zonder box mogelijk. Wil de gebruiker het B2d-gedrag "handmatig oplossen" testen, dan `AUTO_RELEASE=0` en de viewer gebruiken; het rapport markeert dan criterium 5 als "handmatig".

## 7. Bekende beperkingen (README)

- ECS-herstart midden in een run: de service kent de lopende jobs niet meer; reconciliatie via `LastJobId` vangt afgeronde jobs, maar open orders uit de vorige ECS-instantie zijn weg. De driver gaat door (nieuwe batches), de teller `restart_count` maakt het zichtbaar. Volledige herstel-van-ECS-state is B3.
- Geen auth; alleen thuisnetwerk. CSRF op release/resolve (B2b-risico) blijft staan tot B3.
- `speed_factor: 1` in de soak = echte tijd; 72 u ≈ 3 000 moves. Hoger zetten is toegestaan (de ECS-service meet in sim-tijd), maar cuts en MTBF's zijn in sim-seconden.

## 8. Uitvoering

Branch `feat/plan-b2c-deploy-soak` op `feat/plan-b2b-viewer`; PR #7 (base #6). Volgorde: §4.1–4.6 backend-punten (elk met tests) → `terminal_soak` (linkproxy-verhuizing, chaos, driver, report) → deploy/ (Dockerfile, compose, .env, scenario, collect.sh, README) → mini-soak → README-hoofdsectie "Draaien op de NAS (B2c)".

## 9. Beslissingen (zonder vragen aan de gebruiker)

1. **Eén image, vijf services** — simpelste build op de NAS; de QNAP TS-473A is x86-64, geen multi-arch nodig.
2. **Soak-driver i.p.v. runner-in-een-lus** — de runner is een meetinstrument voor één afgebakende run (fresh-ECS-check); de soak heeft continu werk en herstartbestendigheid nodig.
3. **Yard-balans in de driver** (50/50 discharge/load, LOAD alleen uit bezette slots) — lost `YARD_FULL` structureel op; de allocator-`release` in de runner blijft erbij voor lange runner-runs.
4. **`GET /containers` toevoegen** — kleine, leesbare uitbreiding; voorkomt dat de driver van de WS-stream afhangt.
5. **Quarantaine zonder box automatisch opheffen onder auto_release** — nodig voor een onbemande run; met box aan boord blijft menselijke vrijgave vereist (B2d-principe).
6. **Begrensde `known_containers`/records (FIFO 10 000)** — zonder dit groeit de ECS lineair; 10 000 ≫ wat 72 u oplevert, dus DUPLICATE-detectie blijft in de praktijk volledig.
7. **Mini-soak van 10 min als test** — de echte 48–72 u kan hier niet (geen Docker-daemon, sessieprocessen worden gestopt); de gebruiker start hem op de NAS en draait `report.py`.
8. **Geen Kubernetes/Portainer/Traefik** — Container Station met een compose-bestand is genoeg.
