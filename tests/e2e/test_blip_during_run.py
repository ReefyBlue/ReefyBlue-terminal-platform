"""Link blips during a full OPC UA run over HTTP (B2d spec §8): the run still completes.

(a) sim <-> gateway: the gateway reaches the sim through a ``LinkProxy`` (all upstream
    slots proxied, see ``sim_and_gateway(proxied_sts=True)``); only the route to STS-01 is
    cut, so the gateway reports STS-01 OFFLINE while the ECS link stays up.
(b) ECS <-> gateway: the ECS service connects to the gateway through a one-route
    ``LinkProxy``; a cut drops its whole link (every device OFFLINE via LINK_LOST) while
    its HTTP API keeps answering from cache. The runner's own gateway connection is direct.

Both: ECS service with ``--auto-release``, the runner (``run_once_http``) in a background
thread, cut ``CUT_AFTER_S`` wall seconds after the run was started, restore ``CUT_FOR_S``
later; ``GET /status`` is polled while cut (``offline > 0``) and after the restore until
``offline == 0`` again, while the run is still going: after the run the runner's ``Stop``
ends the sim, so every device goes OFFLINE then.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from opcua_procs import ROOT, LinkProxy, ecs_service, free_port, sim_and_gateway
from terminal_runner.run import run_once_http
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from test_service_small import SMALL

log = logging.getLogger(__name__)

CUT_AFTER_S = 10.0
CUT_FOR_S = 3.0
RECOVER_S = 15.0  # gateway RECONNECT_S (5 s) / ECS CONNECT_RETRY_S (2 s) plus margin
RUN_JOIN_S = 100.0


def _status(url: str) -> dict:
    return httpx.get(f"{url}/status", timeout=2.0).json()


def _run_with_blip(scenario, *, ecs_url: str, gateway: str,
                   cut: Callable[[], None], restore: Callable[[], None]) -> tuple[dict, int, float | None]:
    """Run in a thread, blip the link; return ``(kpis, max offline while cut, wall seconds
    from restore until ``offline == 0`` or None if not seen within ``RECOVER_S``)``."""
    result: dict = {}

    def run() -> None:
        try:
            result["k"] = run_once_http(scenario, load_layout(ROOT / "terminal.yaml"), scenario.seed,
                                        ecs=ecs_url, gateway=gateway)
        except BaseException as e:  # noqa: BLE001 - re-raised in the test thread
            result["error"] = e

    t0 = time.monotonic()
    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    time.sleep(CUT_AFTER_S)
    assert runner.is_alive(), result  # the run must still be going when the link drops
    cut()
    t_cut = time.monotonic()
    log.info("link cut at %.1f s wall", t_cut - t0)
    max_offline = 0
    while time.monotonic() - t_cut < CUT_FOR_S:
        try:
            max_offline = max(max_offline, int(_status(ecs_url).get("offline", 0)))
        except httpx.HTTPError as e:
            log.warning("GET /status while cut failed: %s", e)
        time.sleep(0.2)
    restore()
    t_restore = time.monotonic()
    log.info("link restored at %.1f s wall", t_restore - t0)
    recovered = None
    while recovered is None and time.monotonic() - t_restore < RECOVER_S and runner.is_alive():
        try:
            if int(_status(ecs_url).get("offline", -1)) == 0:
                recovered = time.monotonic() - t_restore
        except httpx.HTTPError as e:
            log.warning("GET /status after restore failed: %s", e)
        time.sleep(0.2)
    runner.join(RUN_JOIN_S)
    assert not runner.is_alive(), f"run did not finish within {RUN_JOIN_S} s after the blip"
    if "error" in result:
        raise result["error"]
    log.info("run with blip: %s (%.1f s wall, max offline while cut %d, offline 0 again %s s after restore)",
             result["k"], time.monotonic() - t0, max_offline,
             None if recovered is None else f"{recovered:.1f}")
    return result["k"], max_offline, recovered


def _assert_resolved_completed(ecs_log: str) -> None:
    """The cut hits a running job (seed 7, cut at 10 s): the auto-release path that counts
    its unknown outcome as completed (B2d spec §5.1) must have run, and nothing failed."""
    text = Path(ecs_log).read_text()
    assert re.search(r"resolve \S+ on \S+: completed", text), "no unknown outcome resolved as completed"
    assert not re.search(r"resolve \S+ on \S+: failed", text)


def _assert_completed(k: dict) -> None:
    msg = f"KPIs: {k}"
    assert k["containers_done"] == 20, msg
    assert k["containers_failed"] == 0, msg
    assert k["containers_stuck"] == 0, msg
    assert k["timed_out"] == 0, msg


@pytest.mark.e2e_opcua
def test_sim_gateway_blip_on_sts_run_completes(tmp_path):
    path = tmp_path / "small.yaml"
    path.write_text(SMALL)
    scenario = load_scenario(path)
    t0 = time.monotonic()
    with sim_and_gateway(path, n_sts=1, n_agv=2, log_dir=tmp_path, proxied_sts=True) as procs, \
            ecs_service(procs["endpoint"], log_dir=tmp_path, auto_release=True) as ecs:
        proxy, sts = procs["proxy"], procs["sts_port"]
        k, max_offline, recovered = _run_with_blip(
            scenario, ecs_url=ecs["url"], gateway=procs["endpoint"],
            cut=lambda: proxy.cut([sts]), restore=lambda: proxy.restore([sts]))
    assert procs["returncodes"] == (0, 0), procs["logs"]
    assert ecs["returncode"] == 0, ecs["log"]
    _assert_completed(k)
    _assert_resolved_completed(ecs["log"])
    assert max_offline > 0, "gateway never reported STS-01 OFFLINE while its link was cut"
    assert recovered is not None, f"offline not back to 0 within {RECOVER_S} s of the restore"
    assert time.monotonic() - t0 < 120


@pytest.mark.e2e_opcua
def test_ecs_gateway_blip_run_completes(tmp_path):
    path = tmp_path / "small.yaml"
    path.write_text(SMALL)
    scenario = load_scenario(path)
    t0 = time.monotonic()
    with sim_and_gateway(path, n_sts=1, n_agv=2, log_dir=tmp_path) as procs:
        gw_port = int(procs["endpoint"].rsplit(":", 1)[1])
        proxy = LinkProxy({(listen := free_port()): gw_port}).start()
        try:
            with ecs_service(f"opc.tcp://127.0.0.1:{listen}", log_dir=tmp_path, auto_release=True) as ecs:
                k, max_offline, recovered = _run_with_blip(
                    scenario, ecs_url=ecs["url"], gateway=procs["endpoint"],
                    cut=proxy.cut, restore=proxy.restore)
        finally:
            proxy.stop()
    assert procs["returncodes"] == (0, 0), procs["logs"]
    assert ecs["returncode"] == 0, ecs["log"]
    _assert_completed(k)
    _assert_resolved_completed(ecs["log"])
    assert max_offline > 0, "ECS service never reported its devices OFFLINE while its link was cut"
    assert recovered is not None, f"offline not back to 0 within {RECOVER_S} s of the restore"
    assert time.monotonic() - t0 < 120
