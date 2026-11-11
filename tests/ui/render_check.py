"""Visual check of the viewer (B2b spec §7): a PNG of ``/ui/`` against a live stack.

Usage::

    uv run python tests/ui/render_check.py docs/superpowers/reports/plan-b2b/screenshot.png

Starts sim + gateway + ECS service (``auto_release`` off) on ``tests/ui/faults_fast.yaml``
(the ``baseline.yaml`` fleet with AGV faults every few sim minutes), runs the runner in a
daemon thread so the clock goes and the AGVs work, waits until at least 60 s sim time have
passed with two AGVs BUSY (and, for up to 30 s more, for one alarm so the alarm panel has a
row), opens ``/ui/`` at 1400×800, waits for ``window.__viewer.ready`` and writes the
screenshot to argv[1]. Not a pytest test: the PNG is judged by eye against
``docs/superpowers/references/b2b-viewer/``.

Browser: ``VIEWER_BROWSER`` names a Chrome/Chromium executable (launched with
``--no-sandbox``), as in ``tests/ui/conftest.py``; otherwise Playwright's own chromium.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
from opcua_procs import ROOT, ecs_service, sim_and_gateway

SCENARIO = Path(__file__).resolve().parent / "faults_fast.yaml"
MIN_SIM_S = 60.0
MIN_BUSY_AGV = 2
WORK_WAIT_S = 60.0  # wall seconds for MIN_SIM_S and MIN_BUSY_AGV
ALARM_WAIT_S = 30.0  # wall seconds more for an alarm row (optional)
VIEWPORT = {"width": 1400, "height": 800}


def _get(url: str):
    try:
        resp = httpx.get(url, timeout=2.0)
        return resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def _working(url: str) -> bool:
    status = _get(f"{url}/status") or {}
    equipment = _get(f"{url}/equipment") or []
    busy = sum(1 for e in equipment if e["kind"] == "AGV" and e["state"] == "BUSY")
    return (status.get("sim_time") or 0) >= MIN_SIM_S and busy >= MIN_BUSY_AGV


def _wait(pred, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while not pred():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], "\nusage: render_check.py OUT.png", file=sys.stderr)
        return 2
    out_png = Path(argv[1]).resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)

    from terminal_runner.run import run_once_http
    from terminal_sim.layout import load_layout
    from terminal_sim.scenario import load_scenario

    scenario = load_scenario(SCENARIO)
    log_dir = Path(tempfile.mkdtemp(prefix="render_check_"))
    with sim_and_gateway(SCENARIO, n_sts=2, n_agv=6, log_dir=log_dir) as procs, \
            ecs_service(procs["endpoint"], log_dir=log_dir, auto_release=False) as ecs:
        url = ecs["url"]
        runner: dict = {}

        def run() -> None:
            try:
                run_once_http(scenario, load_layout(ROOT / "terminal.yaml"), scenario.seed,
                              ecs=url, gateway=procs["endpoint"])
            except BaseException as e:  # noqa: BLE001 - reported if the AGVs never work; the stack stops under it at exit
                runner["error"] = e

        threading.Thread(target=run, daemon=True).start()
        if not _wait(lambda: _working(url), WORK_WAIT_S):
            print(f"no {MIN_BUSY_AGV} BUSY AGVs after {MIN_SIM_S} s sim within {WORK_WAIT_S} s; "
                  f"runner: {runner.get('error')!r}; logs in {log_dir}", file=sys.stderr)
            return 1
        has_alarm = _wait(lambda: bool(_get(f"{url}/alarms")), ALARM_WAIT_S)

        # Playwright's sync API after the helpers' asyncio.run() calls (startup is done)
        pw = sync_playwright().start()
        try:
            exe = os.environ.get("VIEWER_BROWSER")
            browser = (pw.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe
                       else pw.chromium.launch())
            try:
                page = browser.new_page(viewport=VIEWPORT)
                page.goto(url + "/ui/")
                page.wait_for_function("window.__viewer && window.__viewer.ready", timeout=15_000)
                page.wait_for_timeout(1500)  # one more poll so positions and KPIs are current
                page.screenshot(path=str(out_png))
                sim_time = page.evaluate("window.__viewer.state.status && window.__viewer.state.status.sim_time")
            finally:
                browser.close()
        finally:
            pw.stop()
    print(f"wrote {out_png} (sim_time {sim_time}, alarm {'yes' if has_alarm else 'no'}; logs in {log_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
