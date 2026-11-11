"""Browser tests of the viewer (``e2e_ui``) against a live sim + gateway + ECS service."""

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
