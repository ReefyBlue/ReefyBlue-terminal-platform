"""Fixtures for the viewer browser tests (``e2e_ui``): a live sim + gateway + ECS service and
a Playwright page.

Browser: ``VIEWER_BROWSER`` names a Chrome/Chromium executable to drive (for machines where
Playwright's own chromium download is unavailable); otherwise Playwright's bundled chromium
(``uv run playwright install chromium``). A browser that will not launch skips the tests.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Error, Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
from opcua_procs import ROOT, LinkProxy, ecs_service, free_port, sim_and_gateway

SCENARIO = ROOT / "scenarios" / "baseline.yaml"  # 2 STS + 6 AGV, the full terminal.yaml fleet
FAULTS_FAST = Path(__file__).resolve().parent / "faults_fast.yaml"  # AGV mtbf 600 s / mttr 120 s sim
FAULT_WAIT_S = 90.0


@pytest.fixture(scope="module")
def viewer_url(tmp_path_factory) -> Iterator[str]:
    log_dir = tmp_path_factory.mktemp("viewer")
    with sim_and_gateway(SCENARIO, n_sts=2, n_agv=6, log_dir=log_dir) as procs, \
            ecs_service(procs["endpoint"], log_dir=log_dir, auto_release=False) as ecs:
        yield ecs["url"]


@pytest.fixture(scope="module")
def fault_agv(tmp_path_factory) -> Iterator[str]:
    """URL of an ECS service (``auto_release=False``) over its own sim + gateway running
    ``faults_fast.yaml``; yields once ``GET /alarms`` holds a ``NEEDS_RELEASE`` for an AGV.

    The runner (``run_once_http``) runs in a daemon thread so the sim clock starts and the
    AGVs work; the ``FaultInjector`` faults an AGV within a few sim minutes and the
    dispatcher raises ``NEEDS_RELEASE`` for it on the FAULT (B2d). The processes live in a
    thread of their own: the helpers call ``asyncio.run()``, which fails in this thread once
    Playwright's sync API (``browser``, possibly started by an earlier test) holds its loop."""
    from terminal_runner.run import run_once_http
    from terminal_sim.layout import load_layout
    from terminal_sim.scenario import load_scenario

    log_dir = tmp_path_factory.mktemp("viewer_faults")
    scenario = load_scenario(FAULTS_FAST)
    ready = threading.Event()
    stop = threading.Event()
    out: dict = {}

    def run(url: str, gateway: str) -> None:
        try:
            out["kpis"] = run_once_http(scenario, load_layout(ROOT / "terminal.yaml"), scenario.seed,
                                        ecs=url, gateway=gateway)
        except BaseException as e:  # noqa: BLE001 - reported if no fault shows up
            out["run_error"] = e

    def lifetime() -> None:
        try:
            with sim_and_gateway(FAULTS_FAST, n_sts=2, n_agv=6, log_dir=log_dir) as procs, \
                    ecs_service(procs["endpoint"], log_dir=log_dir, auto_release=False) as ecs:
                url = ecs["url"]
                threading.Thread(target=run, args=(url, procs["endpoint"]), daemon=True).start()
                deadline = time.monotonic() + FAULT_WAIT_S
                while not _agv_needs_release(url):
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"no NEEDS_RELEASE on an AGV within {FAULT_WAIT_S} s "
                                           f"(runner: {out}); see {procs['logs']}, {ecs['log']}")
                    time.sleep(0.5)
                out["url"] = url
                ready.set()
                stop.wait()
        except BaseException as e:  # noqa: BLE001 - re-raised in the test thread
            out["error"] = e
        finally:
            ready.set()

    proc_thread = threading.Thread(target=lifetime, daemon=True)
    proc_thread.start()
    ready.wait()
    if "error" in out:
        raise out["error"]
    try:
        yield out["url"]
    finally:
        stop.set()
        proc_thread.join(60)


@pytest.fixture(scope="module")
def proxied(tmp_path_factory) -> Iterator[tuple[str, LinkProxy]]:
    """``(url, proxy)``: an ECS service (``auto_release=False``) over its own sim + gateway
    running ``baseline.yaml``, connected to the gateway through a one-route ``LinkProxy``
    (as in ``tests/e2e/test_blip_during_run.py`` (b)). ``proxy.cut()`` drops the ECS's whole
    equipment link (every device OFFLINE via LINK_LOST; ``GET /equipment`` still answers 200
    from the cache, every device OFFLINE) while its HTTP API and WS /events keep running; ``proxy.restore()`` lets it reconnect.

    The runner runs in a daemon thread (direct to the gateway) so the sim clock goes and the
    WS carries ``equipment`` messages; processes live in a thread of their own for the same
    ``asyncio.run()`` reason as ``fault_agv``."""
    from terminal_runner.run import run_once_http
    from terminal_sim.layout import load_layout
    from terminal_sim.scenario import load_scenario

    log_dir = tmp_path_factory.mktemp("viewer_link")
    scenario = load_scenario(SCENARIO)
    ready = threading.Event()
    stop = threading.Event()
    out: dict = {}

    def run(url: str, gateway: str) -> None:
        try:
            out["kpis"] = run_once_http(scenario, load_layout(ROOT / "terminal.yaml"), scenario.seed,
                                        ecs=url, gateway=gateway)
        except BaseException as e:  # noqa: BLE001 - the run outlives the test; ends at teardown
            out["run_error"] = e

    def lifetime() -> None:
        try:
            with sim_and_gateway(SCENARIO, n_sts=2, n_agv=6, log_dir=log_dir) as procs:
                gw_port = int(procs["endpoint"].rsplit(":", 1)[1])
                proxy = LinkProxy({(listen := free_port()): gw_port}).start()
                try:
                    with ecs_service(f"opc.tcp://127.0.0.1:{listen}", log_dir=log_dir,
                                     auto_release=False) as ecs:
                        threading.Thread(target=run, args=(ecs["url"], procs["endpoint"]), daemon=True).start()
                        out["value"] = (ecs["url"], proxy)
                        ready.set()
                        stop.wait()
                finally:
                    proxy.stop()
        except BaseException as e:  # noqa: BLE001 - re-raised in the test thread
            out["error"] = e
        finally:
            ready.set()

    proc_thread = threading.Thread(target=lifetime, daemon=True)
    proc_thread.start()
    ready.wait()
    if "error" in out:
        raise out["error"]
    try:
        yield out["value"]
    finally:
        stop.set()
        proc_thread.join(60)


def _agv_needs_release(url: str) -> bool:
    try:
        alarms = httpx.get(f"{url}/alarms", timeout=2.0).json()
    except (httpx.HTTPError, ValueError):
        return False
    return any(a["kind"] == "NEEDS_RELEASE" and a["equipment_id"].startswith("AGV-") for a in alarms)


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
