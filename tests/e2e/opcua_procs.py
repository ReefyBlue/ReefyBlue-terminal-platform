"""Spawn ``terminal_sim.serve`` + ``terminal_gateway.serve`` as subprocesses for e2e tests."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
from asyncua import Client
from terminal_model.types import State
from terminal_sim.layout import load_layout
from terminal_soak.chaos import Schedule
from terminal_soak.linkproxy import LinkProxy  # also re-exported for tests

ROOT = Path(__file__).resolve().parents[2]
READY_TIMEOUT_S = 60.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_base_port(n_slots: int = 13) -> int:
    """A base port whose ``n_slots`` successors (equipment + clock server) are free right now
    (best effort)."""
    for _ in range(50):
        base = free_port() % 20000 + 20000
        try:
            socks = []
            for p in range(base, base + n_slots):
                s = socket.socket()
                socks.append(s)
                s.bind(("127.0.0.1", p))
            return base
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise RuntimeError("no free port range")


async def _gateway_ready(endpoint: str, n_equipment: int) -> bool:
    """True once the gateway answers and mirrors ``n_equipment`` devices, none FAULT or
    OFFLINE (OFFLINE: the gateway's placeholder before its first upstream connection), and its
    clock link to the sim's control server is up (``Clock/SpeedFactor`` stays 0.0 until the
    gateway has read it from the sim, whose speed factor is > 0). Without the clock check a
    ``Start`` right after readiness can be dropped ("Start not forwarded")."""
    client = Client(endpoint, timeout=2.0)
    try:
        await asyncio.wait_for(client.connect(), 2.0)
    except Exception:
        return False
    try:
        idx = await client.get_namespace_index("urn:terminal:model")
        terminal = await client.nodes.objects.get_child(f"{idx}:Terminal")
        states: list[str] = []
        stack = [terminal]
        while stack:
            node = stack.pop()
            for child in await node.get_children():
                name = (await child.read_browse_name()).Name
                if name == "State":
                    states.append(await child.read_value())
                elif name not in ("Clock",) and (await child.read_node_class()).name == "Object":
                    stack.append(child)
        down = {State.FAULT.value, State.OFFLINE.value}
        if len(states) != n_equipment or any(s in down for s in states):
            return False
        clock = await terminal.get_child(f"{idx}:Clock")
        return await (await clock.get_child(f"{idx}:SpeedFactor")).read_value() > 0
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


def _stop(proc: subprocess.Popen) -> int:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return -9


@contextlib.contextmanager
def sim_and_gateway(
    scenario: Path, *, n_sts: int, n_agv: int, log_dir: Path, proxied_sts: bool = False,
    extra_sim_args: list[str] | None = None, layout: Path = ROOT / "terminal.yaml",
) -> Iterator[dict]:
    """Start sim then gateway on free ports; yield ``{"endpoint", "returncodes"}``.

    ``proxied_sts``: the gateway's upstream links run through a ``LinkProxy`` (gateway base
    port P, every slot ``P+i -> B+i`` of the sim's base port B, control slot
    ``n_equipment`` included);
    ``info["proxy"]`` is it and ``info["sts_port"]`` the proxy port of STS-01 (slot 0), so
    ``proxy.cut([sts_port])`` drops only the gateway's link to STS-01.

    ``extra_sim_args`` are appended to the sim's command line (e.g. ``["--autostart"]``).

    ``layout`` is passed to the sim (``--layout``) and the gateway (``--config``); the fleet
    the gateway mirrors is ``n_sts``/``n_agv`` plus one ASC per block of that layout (two on a
    v2 layout: WS and LS), and
    sim and gateway put the clock server on ``base + n_equipment``.

    On exit both get SIGTERM; their exit codes land in ``returncodes`` (sim, gateway).
    """
    lay = load_layout(layout)
    n_equipment = n_sts + n_agv + len(lay.blocks) * (1 if lay.terminal is None else 2)
    n_slots = n_equipment + 1  # + the clock server
    base = free_base_port(n_slots)
    gw_base = base
    proxy = None
    if proxied_sts:
        gw_base = free_base_port(n_slots)
        while abs(gw_base - base) < n_slots:
            gw_base = free_base_port(n_slots)
        proxy = LinkProxy({gw_base + i: base + i for i in range(n_slots)}).start()
    gw_port = free_port()
    sim_log = (log_dir / "sim.log").open("w")
    gw_log = (log_dir / "gateway.log").open("w")
    sim = subprocess.Popen(
        [sys.executable, "-m", "terminal_sim.serve", str(scenario), "--layout", str(layout),
         "--base-port", str(base), "--host", "127.0.0.1", *(extra_sim_args or [])],
        cwd=ROOT, stdout=sim_log, stderr=subprocess.STDOUT)
    gw = subprocess.Popen(
        [sys.executable, "-m", "terminal_gateway.serve", "--config", str(layout),
         "--port", str(gw_port), "--host", "127.0.0.1", "--upstream-host", "127.0.0.1",
         "--base-port", str(gw_base), "--n-sts", str(n_sts), "--n-agv", str(n_agv)],
        cwd=ROOT, stdout=gw_log, stderr=subprocess.STDOUT)
    endpoint = f"opc.tcp://127.0.0.1:{gw_port}"
    info = {"endpoint": endpoint, "returncodes": None, "logs": (sim_log.name, gw_log.name),
            "proxy": proxy, "sts_port": gw_base if proxy else None}
    try:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not asyncio.run(_gateway_ready(endpoint, n_equipment)):
            if sim.poll() is not None or gw.poll() is not None:
                raise RuntimeError(f"process died during startup; see {info['logs']}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"gateway {endpoint} not ready within {READY_TIMEOUT_S} s")
            time.sleep(0.5)
        yield info
    finally:
        info["returncodes"] = (_stop(sim), _stop(gw))
        if proxy is not None:
            proxy.stop()
        sim_log.close()
        gw_log.close()


def _fleet_up(url: str) -> bool:
    resp = httpx.get(f"{url}/equipment", timeout=2.0)
    if resp.status_code != 200:
        return False
    eq = resp.json()
    return bool(eq) and all(e["state"] not in (State.FAULT.value, State.OFFLINE.value) for e in eq)


@contextlib.contextmanager
def ecs_service(
    gateway_endpoint: str, *, log_dir: Path, auto_release: bool = False,
    layout: Path = ROOT / "terminal.yaml",
) -> Iterator[dict]:
    """Start ``terminal_ecs_service.serve`` on a free port with ``--layout layout``
    (``--auto-release`` when ``auto_release``); yield ``{"url", "returncode", "log"}``.

    Waits for ``GET /health`` 200 and no device FAULT or OFFLINE in ``GET /equipment``; on
    exit SIGTERM and the exit code lands in ``returncode``.
    """
    port = free_port()
    log = (log_dir / "ecs.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "terminal_ecs_service.serve", "--gateway", gateway_endpoint,
         "--layout", str(layout), "--host", "127.0.0.1", "--port", str(port),
         *(["--auto-release"] if auto_release else [])],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    info = {"url": url, "returncode": None, "log": log.name}
    try:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"ECS service died during startup; see {log.name}")
            try:
                if httpx.get(f"{url}/health", timeout=2.0).status_code == 200 and _fleet_up(url):
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"ECS service {url} not healthy within {READY_TIMEOUT_S} s")
            time.sleep(0.5)
        yield info
    finally:
        info["returncode"] = _stop(proc)
        log.close()


@contextlib.contextmanager
def chaos_proxy(target_port: int, every: float, for_s: float) -> Iterator[dict]:
    """A ``LinkProxy`` on a free port in front of ``127.0.0.1:target_port`` whose link a
    ``chaos.Schedule(every, for_s)`` cuts in a daemon thread (first cut ``every`` s after
    entry). Yields ``{"endpoint", "proxy", "events", "stop_cuts"}``: ``endpoint`` is the
    proxied ``opc.tcp://`` URL, ``events`` the ``(monotonic, "cut"|"restore")`` log and
    ``stop_cuts()`` ends the schedule with the link up (a cut in progress is restored)
    while the proxy keeps serving. On exit the schedule stops and the proxy closes."""
    listen = free_port()
    proxy = LinkProxy({listen: target_port}).start()
    stop = threading.Event()
    events: list[tuple[float, str]] = []
    thread = threading.Thread(
        target=Schedule(every, for_s).run, args=(proxy, stop),
        kwargs={"on_event": lambda e: events.append((time.monotonic(), e))}, daemon=True)
    thread.start()

    def stop_cuts() -> None:
        stop.set()
        thread.join(timeout=for_s + 5.0)

    try:
        yield {"endpoint": f"opc.tcp://127.0.0.1:{listen}", "proxy": proxy, "events": events,
               "stop_cuts": stop_cuts}
    finally:
        stop_cuts()
        proxy.stop()
