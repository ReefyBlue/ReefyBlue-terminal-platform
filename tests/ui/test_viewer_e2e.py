"""Browser tests of the viewer (``e2e_ui``) against a live sim + gateway + ECS service."""

import time

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


RELEASE_ROW = "#alarms li[data-key^='NEEDS_RELEASE|AGV-']"


def test_panel_shows_alarms_and_release_works(fault_agv, page):
    page.goto(fault_agv + "/ui/")
    page.wait_for_selector(RELEASE_ROW, timeout=60_000)
    eid = page.get_attribute(RELEASE_ROW, "data-key").split("|")[1]
    rows = f"#alarms li[data-key^='NEEDS_RELEASE|{eid}|']"
    button = f"#alarms button.release[data-equipment='{eid}']"
    assert page.locator(button).count() == 1  # Review Focus 4
    assert "null" not in page.inner_text("#alarms")  # Review Focus 3
    # the ECS refuses a release while the AGV is still FAULT (409 RELEASE_REFUSED): the row
    # shows the error for 5 s and re-enables the button, so click again until it is gone
    deadline = time.monotonic() + 30
    while page.locator(rows).count() and time.monotonic() < deadline:
        if page.locator(button).count() and page.locator(button).is_enabled():
            page.click(button)
        page.wait_for_timeout(500)
    page.wait_for_function(f"document.querySelectorAll(\"{rows}\").length === 0", timeout=15_000)


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
    assert page.locator("#alarms button.release[data-equipment='AGV-02']").count() == 0


def test_refused_action_shows_error_for_5_s(page, viewer_url):
    page.goto(viewer_url + "/ui/")
    page.wait_for_function("window.__viewer && window.__viewer.ready", timeout=15_000)
    page.evaluate("""() => { const v = window.__viewer;
        v.model.apply(v.state, {type:'alarm', action:'raised', kind:'NEEDS_RELEASE', equipment_id:'XX-99', job_id:null, since_sim_time:1, detail:'', sim_time:1}); }""")
    page.click("#alarms button.release[data-equipment='XX-99']")
    error = "#alarms li[data-key='NEEDS_RELEASE|XX-99|'] .error"
    page.wait_for_selector(error, timeout=5_000)
    assert page.inner_text(error).startswith("UNKNOWN_EQUIPMENT: ")
    assert page.locator("#alarms button.release[data-equipment='XX-99']").is_enabled()
    page.wait_for_selector(error, state="detached", timeout=7_000)


def test_canvas_click_selects_equipment(page, viewer_url):
    page.goto(viewer_url + "/ui/")
    page.wait_for_function("window.__viewer && window.__viewer.ready", timeout=15_000)
    x, y = page.evaluate("""() => { const v = window.__viewer; const c = document.getElementById('map');
        const fit = v.map.fit(v.state.layout, c.clientWidth, c.clientHeight);
        const s = v.state.layout.sts.find((s) => s.equipment_id === 'STS-01'); const r = c.getBoundingClientRect();
        return [r.left + fit.ox + s.x * fit.scale, r.top + fit.oy + 5]; }""")
    page.mouse.click(x, y)
    page.wait_for_selector("#selection dd")
    text = page.inner_text("#selection")
    assert "STS-01" in text and "STS" in text and "IDLE" in text
