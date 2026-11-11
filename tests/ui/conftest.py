"""Fixtures for the viewer browser tests (``e2e_ui``): a live sim + gateway + ECS service and
a Playwright page.

Browser: ``VIEWER_BROWSER`` names a Chrome/Chromium executable to drive (for machines where
Playwright's own chromium download is unavailable); otherwise Playwright's bundled chromium
(``uv run playwright install chromium``). A browser that will not launch skips the tests.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Error, Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
from opcua_procs import ROOT, ecs_service, sim_and_gateway  # noqa: E402

SCENARIO = ROOT / "scenarios" / "baseline.yaml"  # 2 STS + 6 AGV, the full terminal.yaml fleet


@pytest.fixture(scope="module")
def viewer_url(tmp_path_factory) -> Iterator[str]:
    log_dir = tmp_path_factory.mktemp("viewer")
    with sim_and_gateway(SCENARIO, n_sts=2, n_agv=6, log_dir=log_dir) as procs, \
            ecs_service(procs["endpoint"], log_dir=log_dir, auto_release=False) as ecs:
        yield ecs["url"]


@pytest.fixture(scope="module")
def browser(viewer_url) -> Iterator[Browser]:
    # after viewer_url: the process helpers call asyncio.run(), which fails while Playwright's
    # sync API holds the thread's running event loop
    pw = sync_playwright().start()
    exe = os.environ.get("VIEWER_BROWSER")
    try:
        if exe:
            b = pw.chromium.launch(executable_path=exe, args=["--no-sandbox"])
        else:
            b = pw.chromium.launch()
    except Error as e:
        pw.stop()
        pytest.skip(f"no browser for the viewer tests ({e.message.splitlines()[0]}); set VIEWER_BROWSER "
                    "to a Chrome/Chromium executable or run 'uv run playwright install chromium'")
    try:
        yield b
    finally:
        b.close()
        pw.stop()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    ctx = browser.new_context(viewport={"width": 1400, "height": 800})
    pg = ctx.new_page()
    try:
        yield pg
    finally:
        ctx.close()
