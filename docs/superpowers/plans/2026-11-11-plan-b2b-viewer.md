# Plan B2b — Webviewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A browser page served by the ECS service that shows the terminal top view live and lets an operator release equipment and resolve unknown job outcomes.

**Architecture:** New dependency-free package `terminal_viewer` holds static HTML/JS/CSS (vanilla ES module, Canvas 2D). `terminal_ecs_service` gains `GET /layout` and mounts the static dir at `/ui` when the package is importable. The page polls `GET /equipment` at 1 Hz for positions and listens on `WS /events` for status/alarm/link messages; actions go to the existing `release`/`resolve` endpoints.

**Tech Stack:** Python 3.12, FastAPI/Starlette `StaticFiles`, pydantic; browser: ES2020 module, Canvas 2D, fetch, WebSocket. Dev only: Playwright (Python) + chromium for the `e2e_ui` test.

**Spec:** `docs/superpowers/specs/2026-11-11-plan-b2b-viewer-design.md`

## Global Constraints

- No build step, no Node, no CDN: everything ships in the `terminal_viewer` wheel (`static/` included by hatch).
- `terminal_viewer` imports nothing from `terminal_*`; `terminal_ecs_service` imports `terminal_viewer` only inside `_viewer_dir()`.
- Existing API unchanged; B2a/B2d tests stay green (`uv run pytest` default suite).
- UI labels Dutch; codes (`LINK_LOST`, `NEEDS_RELEASE`, `UNKNOWN_OUTCOME`, states) English as in the API.
- State colours: IDLE `#5b8c5a`, BUSY `#2e7d32`, FAULT `#c62828`, OFFLINE `#6d5f8a` (dashed outline), MAINTENANCE `#ef6c00`.
- Poll interval 1000 ms; poll backoff 5 s after an error; WS reconnect 1 s doubling to max 10 s; event log ring 50; action error shown 5 s.
- Commits carry the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_011Q6oL3tzJGQycNuMVuzTtk`.
- Branch `feat/plan-b2b-viewer` (base `feat/plan-b2d-link-loss`).

## Review Focus

1. Layout with STS/blocks whose coordinates exceed `grid_cols*grid_cell_m` (e.g. `terminal.yaml` STS at x=250 on a 400 m quay but grid 20×20 m) — the map must fit by the bounding box of quay length and all specs, not by grid alone (Task 3 test `fit_uses_bounding_box`).
2. `GET /equipment` returning 503 while the gateway is down — the map must dim and keep the last positions, not blank or throw (Task 5 test in `e2e_ui`; Task 3 `model.apply` ignores poll failure).
3. An alarm whose `job_id` is `null` (LINK_LOST, NEEDS_RELEASE without job) — the alarm key and the buttons must not render `null` or call `/jobs/null/resolve` (Task 4 test `panel_alarm_without_job`).
4. Two alarms for the same equipment (`LINK_LOST` and `NEEDS_RELEASE`) — both rows shown, release button only once per equipment (Task 4 test `panel_release_once_per_equipment`).
5. Equipment whose `extra` lacks viewer keys (a vendor adapter without `route`/`loaded_container_id`) — the map must draw a plain rectangle, not throw (Task 3 test `draw_tolerates_missing_extra`).

---

## File structure

- Create `packages/viewer/pyproject.toml`, `packages/viewer/src/terminal_viewer/__init__.py` (`static_dir()`), `packages/viewer/src/terminal_viewer/static/{index.html,viewer.css,viewer.js}`.
- Modify `packages/ecs_service/src/terminal_ecs_service/schemas.py` (+`LayoutOut` family), `app.py` (`/layout`, `/ui` mount, `/` redirect, `_viewer_dir()`), `pyproject.toml` (optional dep `viewer`).
- Modify root `pyproject.toml` (workspace member, dev group, `e2e_ui` marker, Playwright dev dep), `tests/test_architecture.py` (`viewer` in `ALLOWED`, `ecs_service` may import `terminal_viewer`).
- Tests: `tests/ecs_service/test_layout_and_ui.py`, `tests/ui/test_viewer_e2e.py` (`e2e_ui`), `tests/ui/render_check.py` (screenshot script).
- `viewer.js` exposes `window.__viewer = {state, model, map, ready}` so the browser test can assert on pure functions without a JS toolchain.

---

### Task 1: `GET /layout` on the ECS service

**Files:**
- Modify: `packages/ecs_service/src/terminal_ecs_service/schemas.py`
- Modify: `packages/ecs_service/src/terminal_ecs_service/app.py` (after `get_health`)
- Modify: `packages/ecs_service/src/terminal_ecs_service/core.py:59-75` (keep a reference `self.layout = layout`)
- Test: `tests/ecs_service/test_layout_and_ui.py`

**Interfaces:**
- Consumes: `EcsCore(layout, connect=...)`; `terminal_sim.layout.load_layout(Path) -> Layout` with `quay_length_m, grid_cell_m, grid_cols, grid_rows, sts[StsSpec(equipment_id, position, lane_cells)], blocks[BlockSpec(block, asc_id, tp_ws, tp_ws_cell, bays, rows, tiers)], agv_start_cells`.
- Produces: `GET /layout -> LayoutOut {quay_length_m: float, grid_cell_m: float, grid_cols: int, grid_rows: int, sts: [{equipment_id: str, x: float, y: float, lane_cells: [[int,int],[int,int]]}], blocks: [{block: str, asc_id: str, tp_ws: {x,y}, tp_ws_cell: [int,int], bays: int, rows: int, tiers: int}], agv_start_cells: [[int,int]]}`; `core.layout`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ecs_service/test_layout_and_ui.py
from pathlib import Path
import httpx, pytest
from fake_port import FakePort
from terminal_ecs_service.app import create_app
from terminal_ecs_service.core import EcsCore
from terminal_model.types import Kind, Position
from terminal_sim.layout import load_layout

ROOT = Path(__file__).resolve().parents[2]

def _client(layout):
    fake = FakePort({"STS-01": (Kind.STS, Position(100, 0))})
    core = EcsCore(layout, connect=lambda: fake)
    app = create_app(core, run_loop=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

async def test_layout_endpoint_mirrors_terminal_yaml():
    layout = load_layout(ROOT / "terminal.yaml")
    async with _client(layout) as c:
        r = await c.get("/layout")
    assert r.status_code == 200
    body = r.json()
    assert body["quay_length_m"] == 400.0 and body["grid_cell_m"] == 20.0
    assert body["grid_cols"] == 20 and body["grid_rows"] == 6
    assert body["sts"][0] == {"equipment_id": "STS-01", "x": 100.0, "y": 0.0, "lane_cells": [[4, 0], [5, 0]]}
    b = body["blocks"][0]
    assert b["block"] == "A" and b["asc_id"] == "ASC-A" and b["tp_ws"] == {"x": 50.0, "y": 120.0}
    assert b["tp_ws_cell"] == [2, 5] and (b["bays"], b["rows"], b["tiers"]) == (40, 6, 4)
    assert body["agv_start_cells"][0] == [0, 2]

async def test_layout_available_without_equipment_link():
    layout = load_layout(ROOT / "terminal.yaml")
    async with _client(layout) as c:  # core never connected
        assert (await c.get("/layout")).status_code == 200
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/ecs_service/test_layout_and_ui.py -v` → FAIL 404.

- [ ] **Step 3: Implement** — in `schemas.py` add `PositionOut(x: float, y: float)`, `StsOut`, `BlockOut`, `LayoutOut` (fields as in Interfaces; `lane_cells: tuple[tuple[int,int], tuple[int,int]]`, cells as `tuple[int,int]`). In `core.py` store `self.layout = layout` (the existing `LayoutLike` protocol gains the read-only attributes `quay_length_m, grid_cell_m, grid_cols, grid_rows, sts, agv_start_cells`; `_BlockLike` gains `asc_id, tp_ws, tp_ws_cell`). In `app.py` add `@app.get("/layout", response_model=LayoutOut) async def get_layout()` building the model from `core.layout` — no `guarded`, no 503. The existing `tests/ecs_service` fixtures use `SimpleNamespace` layouts without these attributes: `get_layout` must only be exercised with a real `Layout`; do not touch those fixtures.

- [ ] **Step 4: Run** — same command → PASS; `uv run pytest tests/ecs_service -q` → all pass.

- [ ] **Step 5: Commit** — `feat(ecs_service): GET /layout exposes terminal geometry for the viewer`.

---

### Task 2: `terminal_viewer` package and `/ui` mount

**Files:**
- Create: `packages/viewer/pyproject.toml`, `packages/viewer/src/terminal_viewer/__init__.py`, `packages/viewer/src/terminal_viewer/static/index.html` (placeholder: `<title>Terminal viewer</title><canvas id="map"></canvas><aside id="panel"></aside><script type="module" src="viewer.js"></script>`), `static/viewer.css` (empty), `static/viewer.js` (`export {};`).
- Modify: `packages/ecs_service/src/terminal_ecs_service/app.py`, `packages/ecs_service/pyproject.toml`, root `pyproject.toml`, `tests/test_architecture.py`.
- Test: `tests/ecs_service/test_layout_and_ui.py` (append)

**Interfaces:**
- Produces: `terminal_viewer.static_dir() -> pathlib.Path`; `terminal_ecs_service.app._viewer_dir() -> Path | None`; `GET /ui/` (200, `text/html`), `GET /` → 307 `/ui/`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_ui_served_and_root_redirects():
    layout = load_layout(ROOT / "terminal.yaml")
    async with _client(layout) as c:
        r = await c.get("/ui/")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert (await c.get("/ui/viewer.js")).status_code == 200
        root = await c.get("/", follow_redirects=False)
        assert root.status_code == 307 and root.headers["location"] == "/ui/"

async def test_without_viewer_package_no_ui(monkeypatch):
    import terminal_ecs_service.app as appmod
    monkeypatch.setattr(appmod, "_viewer_dir", lambda: None)
    layout = load_layout(ROOT / "terminal.yaml")
    async with _client(layout) as c:
        assert (await c.get("/ui/")).status_code == 404
        assert (await c.get("/", follow_redirects=False)).status_code == 404
```

Also in `tests/test_architecture.py`: add `"viewer": set()` to `ALLOWED`, add `"terminal_viewer"` to the `ecs_service` set, and a test `test_viewer_has_no_python_logic()` asserting `packages/viewer/src/terminal_viewer/__init__.py` is the only `.py` file under the package and that `static/index.html`, `viewer.js`, `viewer.css` exist.

- [ ] **Step 2: Run** → FAIL (ImportError / 404).

- [ ] **Step 3: Implement** — `packages/viewer/pyproject.toml`: hatchling, name `terminal-viewer`, no dependencies, `[tool.hatch.build.targets.wheel] packages = ["src/terminal_viewer"]` (static files inside the package dir are included). `__init__.py`: `def static_dir() -> Path: return Path(__file__).parent / "static"`. Root `pyproject.toml`: add `terminal-viewer` to dev group and `[tool.uv.sources]`; `packages/ecs_service/pyproject.toml`: `[project.optional-dependencies] viewer = ["terminal-viewer"]` + source. In `app.py`: module-level `def _viewer_dir() -> Path | None` (try import → `static_dir()`, except ImportError → None); at the end of `create_app`, `if (d := _viewer_dir()) is not None:` mount `StaticFiles(directory=str(d), html=True)` at `/ui` and add `@app.get("/", include_in_schema=False)` returning `RedirectResponse("/ui/", status_code=307)`; log one `info` line either way. Run `uv sync`.

- [ ] **Step 4: Run** — `uv run pytest tests/ecs_service/test_layout_and_ui.py tests/test_architecture.py -v` → PASS.

- [ ] **Step 5: Commit** — `feat(viewer): terminal_viewer static package mounted at /ui by the ECS service`.

---

### Task 3: Map rendering (`viewer.js`: `model`, `map`) and page skeleton

**Files:**
- Modify: `packages/viewer/src/terminal_viewer/static/{index.html,viewer.css,viewer.js}`
- Test: `tests/ui/test_viewer_e2e.py` (first test), `tests/ui/conftest.py`

**Interfaces:**
- Consumes: `GET /layout` (Task 1), `GET /equipment` items `{equipment_id, kind: "STS"|"AGV"|"ASC", vendor, state, position:{x,y}, current_job_id, energy_kwh, extra}`; extra keys used: AGV `loaded_container_id`, `route: [[col,row],…]`; STS `handover_lane_occupied`; ASC `block_id`, `gantry_position_m`, `ws_tp_occupied`.
- Produces (on `window.__viewer`): `state` = `{layout, equipment: Map<id, item>, alarms: [], status: null, kpis: null, events: [], link: {ws: false, ecs: false, connected: null}, selected: null, dirty: true}`; `model.apply(state, message)`; `model.setEquipment(state, items)`; `map.fit(layout, width, height) -> {scale, ox, oy, worldW, worldH}`; `map.draw(ctx, state, fit)`; `map.hitTest(state, fit, px, py) -> id|null`; `ready: boolean` (true after first successful draw). Constants `COLORS = {IDLE:"#5b8c5a", BUSY:"#2e7d32", FAULT:"#c62828", OFFLINE:"#6d5f8a", MAINTENANCE:"#ef6c00"}`.

- [ ] **Step 1: Test infrastructure** — root `pyproject.toml`: marker `e2e_ui: browser test of the viewer with Playwright (run with -m e2e_ui; needs 'uv run playwright install chromium')`, `addopts = "-m 'not e2e_opcua and not e2e_ui'"`, dev dep `playwright>=1.47`, `pytest-playwright` **not** used (keep plain `sync_playwright` to avoid fixture conflicts with pytest-asyncio auto mode). `tests/ui/conftest.py`: fixture `viewer_url` that starts `sim_and_gateway` + `ecs_service` from `tests/e2e/opcua_procs.py` (reuse, `auto_release=False`) and yields the service URL; fixture `page` from `sync_playwright().start().chromium.launch()`.

- [ ] **Step 2: Write the failing test**

```python
# tests/ui/test_viewer_e2e.py
import pytest
pytestmark = pytest.mark.e2e_ui

def test_map_draws_layout_and_fleet(page, viewer_url):
    page.goto(viewer_url + "/ui/")
    page.wait_for_function("window.__viewer && window.__viewer.ready", timeout=15_000)
    n = page.evaluate("window.__viewer.state.equipment.size")
    assert n == 12  # 2 STS + 6 AGV + 4 ASC in terminal.yaml
    fit = page.evaluate("window.__viewer.map.fit(window.__viewer.state.layout, 1000, 600)")
    assert fit["worldW"] >= 400 and fit["worldH"] >= 120  # bounding box, not grid only  (Review Focus 1)
    assert page.evaluate("""() => { const v = window.__viewer; const s = structuredClone(v.state);
        s.equipment = new Map([["AGV-99", {equipment_id:"AGV-99", kind:"AGV", state:"IDLE", position:{x:1,y:1}, extra:{}}]]);
        const c = document.createElement('canvas'); c.width=100; c.height=100;
        try { v.map.draw(c.getContext('2d'), s, v.map.fit(s.layout,100,100)); return true } catch (e) { return String(e) } }""") is True  # Review Focus 5
```

- [ ] **Step 3: Run** — `uv run pytest -m e2e_ui tests/ui -v` → FAIL (`__viewer` undefined).

- [ ] **Step 4: Implement `viewer.js` (model + map + bootstrap)** — `index.html`: header bar with title "Terminal viewer", `<main><canvas id="map"></canvas><aside id="panel">…</aside></main>`; `viewer.css`: two-column grid (canvas flexible, panel 360 px), system font, white background, light grey grid lines. `map.fit` computes `worldW = max(quay_length_m, grid_cols*grid_cell_m, max x of sts/blocks + grid_cell_m)` and `worldH = max(grid_rows*grid_cell_m, max y + grid_cell_m)`, uniform scale with 20 px margin. `map.draw`: quay band at y∈[0, grid_cell_m/2] (blue `#1e5aa8`), light grid, STS as 12 px-wide bar from y=0 to the lane row with id label and hatched lane cells when `handover_lane_occupied`, blocks as rectangles from `tp_ws` row down to `worldH` with letter, TP cell hatched when `ws_tp_occupied`, ASC as bar at `tp_ws.x - half block width + gantry_position_m` (block width = `bays * 6.5 m`, clamp), AGV as 8×4 m rectangle at `position` with id above, inner filled square when `loaded_container_id`, polyline through `route` cells. Fill colour by `COLORS[state]`, OFFLINE adds `setLineDash([4,3])`. When `state.link.ecs === false` draw everything with `globalAlpha = 0.5`. Bootstrap: fetch `/layout` (retry 5 s on failure, message in canvas), fetch `/equipment` once, `window.__viewer = {state, model, map, ready:false}`, rAF loop draws when `dirty`, sets `ready = true` after the first draw. `model.setEquipment` replaces the Map and sets `dirty`.

- [ ] **Step 5: Run** → PASS. Also `uv run pytest -q` (default suite still green; `tests/ui` deselected).

- [ ] **Step 6: Commit** — `feat(viewer): canvas top view of quay, STS, AGV and blocks from /layout and /equipment`.

---

### Task 4: Panel — link banner, KPIs, alarms with actions, event log, selection

**Files:**
- Modify: `viewer.js` (`panel`, `api`), `index.html`, `viewer.css`
- Test: `tests/ui/test_viewer_e2e.py` (append)

**Interfaces:**
- Consumes: `GET /alarms` items `{kind, equipment_id, job_id|null, since_sim_time, detail}`, `GET /status` (`connected, offline, needs_release, unknown_outcomes, done, failed, open, …`), `GET /kpis` (dict of floats), `POST /equipment/{id}/release` → 200 `{…}` | 404 `{code:"UNKNOWN_EQUIPMENT"}` | 409 `{code:"RELEASE_REFUSED", message}` | 503; `POST /jobs/{id}/resolve {"outcome":"completed"|"failed"}` → 200 | 404 `UNKNOWN_JOB` | 422 | 503.
- Produces: `api.release(id) / api.resolve(jobId, outcome)` throwing `ApiError{status, code, message}`; `panel.render(state)`; DOM ids `#banner`, `#kpis`, `#alarms` (one `<li data-key="kind|equipment_id|job_id">` per alarm, buttons `button.release[data-equipment]`, `button.resolve[data-job][data-outcome]`), `#events`, `#selection`; `model.alarmKey(a) -> "kind|equipment_id|job_id-or-"` (empty string for null).

- [ ] **Step 1: Write the failing tests**

```python
def test_panel_shows_alarms_and_release_works(page, viewer_url, fault_agv):
    # fault_agv: conftest fixture that injects a FAULT on AGV-01 via the sim's fault hook (see opcua_procs / FaultInjector) so NEEDS_RELEASE appears once the AGV is repaired
    page.goto(viewer_url + "/ui/")
    page.wait_for_selector("#alarms li[data-key^='NEEDS_RELEASE|AGV-01|']", timeout=60_000)
    rows = page.locator("#alarms li[data-key*='|AGV-01|']")
    assert page.locator("#alarms button.release[data-equipment='AGV-01']").count() == 1  # Review Focus 4
    assert "null" not in page.inner_text("#alarms")  # Review Focus 3
    page.click("#alarms button.release[data-equipment='AGV-01']")
    page.wait_for_function("document.querySelectorAll(\"#alarms li[data-key*='|AGV-01|']\").length === 0", timeout=15_000)

def test_resolve_buttons_only_for_unknown_outcome(page, viewer_url):
    page.goto(viewer_url + "/ui/")
    page.wait_for_function("window.__viewer && window.__viewer.ready", timeout=15_000)
    page.evaluate("""() => { const v = window.__viewer;
        v.model.apply(v.state, {type:'alarm', action:'raised', kind:'LINK_LOST', equipment_id:'STS-01', job_id:null, since_sim_time:1, detail:'x', sim_time:1});
        v.model.apply(v.state, {type:'alarm', action:'raised', kind:'UNKNOWN_OUTCOME', equipment_id:'AGV-02', job_id:'J-7', since_sim_time:1, detail:'y', sim_time:1}); }""")
    page.wait_for_selector("#alarms li[data-key='UNKNOWN_OUTCOME|AGV-02|J-7']")
    assert page.locator("#alarms li[data-key='LINK_LOST|STS-01|'] button.resolve").count() == 0
    assert page.locator("#alarms li[data-key='UNKNOWN_OUTCOME|AGV-02|J-7'] button.resolve").count() == 2
    assert page.locator("#alarms button.release[data-equipment='STS-01']").count() == 1
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — `api`: thin fetch wrappers, non-2xx → `ApiError(status, body.code ?? body.detail ?? "HTTP "+status, body.message ?? "")`. `model.apply` handles `alarm raised|cleared` (by `alarmKey`), `status` (store; triggers `GET /kpis` refetch in main), `equipment` (push to `events` ring 50, apply `new_state` to the equipment item if present), `job` (events), `link` (`link.connected`). `panel.render`: banner text/colour — green "ECS ↔ gateway verbonden", red "Gateway verbroken" when `link.connected === false`, grey "Viewer ↔ ECS verbroken" when `!link.ws && !link.ecs`; KPI table (`done`, `failed`, `open`, `offline`, `needs_release`, `unknown_outcomes`, plus `kpis` entries); alarms oldest first with Dutch labels (LINK_LOST → "Verbinding verbroken", NEEDS_RELEASE → "Vrijgave nodig", UNKNOWN_OUTCOME → "Uitkomst onbekend"), one `Vrijgeven` button per equipment (rendered on the first alarm row for that equipment only), `Voltooid`/`Mislukt` buttons only when `kind === "UNKNOWN_OUTCOME"`; click → disable button → call api → on `ApiError` show `code: message` in the row for 5 s and re-enable. Event log newest first, 50 rows (`sim_time`, type, equipment, job/new_state/reason). Selection: canvas click → `map.hitTest` → `#selection` lists id, kind, state, job and `extra` key/values. Legend block: three labels "Kade (berth)", "Rijpaden (internal path)", "Stack (yard)".

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit** — `feat(viewer): side panel with link banner, KPIs, alarm actions, event log and selection`.

---

### Task 5: Live stream and polling with reconnect; dimming on lost link

**Files:**
- Modify: `viewer.js` (`stream`, `poll`, main)
- Test: `tests/ui/test_viewer_e2e.py` (append)

**Interfaces:**
- Consumes: `WS /events`; `GET /equipment` (503 when not connected); `LinkProxy.cut()/restore()` from `tests/e2e/opcua_procs.py` (conftest fixture `link` yields the proxy between ECS and gateway as in `test_blip_during_run.py`).
- Produces: `stream.start(state, onMessage)` with reconnect 1 s → ×2 → max 10 s, sets `state.link.ws`; `poll.start(state)` every 1000 ms, on non-200 sets `state.link.ecs = false` and waits 5 s, on 200 sets `true`.

- [ ] **Step 1: Write the failing test**

```python
def test_cut_shows_offline_and_dims_then_recovers(page, viewer_url, link):
    page.goto(viewer_url + "/ui/")
    page.wait_for_function("window.__viewer && window.__viewer.ready", timeout=15_000)
    link.cut()
    page.wait_for_selector("#alarms li[data-key^='LINK_LOST|']", timeout=30_000)
    assert "verbroken" in page.inner_text("#banner").lower()
    assert page.evaluate("[...window.__viewer.state.equipment.values()].every(e => e.state === 'OFFLINE')")
    assert page.evaluate("window.__viewer.state.equipment.size") == 12  # last positions kept (Review Focus 2)
    link.restore()
    page.wait_for_function("document.querySelectorAll(\"#alarms li[data-key^='LINK_LOST|']\").length === 0", timeout=60_000)
    assert "verbonden" in page.inner_text("#banner").lower()
```

- [ ] **Step 2: Run** → FAIL (no WS/poll yet, states never change).

- [ ] **Step 3: Implement** — `stream`: `new WebSocket((location.protocol === 'https:' ? 'wss' : 'ws') + '://' + location.host + '/events')`; `onmessage` → `model.apply`; `onclose` → schedule reconnect with the backoff; on open reset backoff and refetch `/alarms`, `/status`, `/kpis` (to resync after a gap). `poll`: `setTimeout` chain, 1000 ms, `GET /equipment` → `model.setEquipment`; failure → `link.ecs=false`, next attempt in 5000 ms. Map draws at `globalAlpha 0.5` when `link.ecs === false || link.connected === false`. Note that during a gateway cut `GET /equipment` returns 503 (B2d: port marks all OFFLINE but the service answers 503 when `connected` is false) — the test therefore reads states from the last WS `equipment` messages (`new_state: OFFLINE`), which `model.apply` applies.

- [ ] **Step 4: Run** → PASS. Run the whole `-m e2e_ui` file twice; record both results.

- [ ] **Step 5: Commit** — `feat(viewer): WS event stream with reconnect and 1 Hz equipment polling`.

---

### Task 6: Screenshot check and documentation

**Files:**
- Create: `tests/ui/render_check.py` (script: starts sim+gateway+service via `opcua_procs`, submits the small scenario with `terminal_tos_stub.submit` for 60 s sim, screenshots `/ui/` to the path given as argv[1]).
- Create: `docs/superpowers/reports/plan-b2b/screenshot.png` (output of the script).
- Modify: `README.md` (section "Webviewer (B2b)"), `packages/ecs_service/src/terminal_ecs_service/serve.py` (log line with the `/ui/` URL at startup).

- [ ] **Step 1: Run the script** — `uv run python tests/ui/render_check.py docs/superpowers/reports/plan-b2b/screenshot.png`; open the PNG with Read and compare against `docs/superpowers/references/b2b-viewer/ecs-screen-reference.png`: quay on top, STS bars, AGVs with box indicator, blocks with TP+ASC, panel on the right. Fix drawing issues found; re-run.
- [ ] **Step 2: README** — how to open (`http://<nas>:8000/ui/`), what each colour means, the two actions, the `e2e_ui` marker and `uv run playwright install chromium`, the limitation (no auth; positions 1 Hz).
- [ ] **Step 3: Run** `uv run pytest -q` (default) and `uv run pytest -m e2e_ui tests/ui -q` → all pass.
- [ ] **Step 4: Commit** — `docs: Plan B2b README section and viewer screenshot`.
