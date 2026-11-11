"""Spawn ``terminal_sim.serve`` + ``terminal_gateway.serve`` as subprocesses for e2e tests."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import struct
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

ROOT = Path(__file__).resolve().parents[2]
READY_TIMEOUT_S = 60.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_base_port() -> int:
    """A base port whose 13 successors are free right now (best effort)."""
    for _ in range(50):
        base = free_port() % 20000 + 20000
        try:
            socks = []
            for p in range(base, base + 13):
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
    OFFLINE (OFFLINE: the gateway's placeholder before its first upstream connection)."""
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
        return len(states) == n_equipment and not any(s in down for s in states)
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


class LinkProxy:
    """Threaded TCP proxy (``listen port -> target port`` on ``host``) whose routes can be
    cut and restored while both ends keep running; a sync twin of ``tests/gateway/tcp_proxy.py``
    for e2e tests whose main thread blocks in the runner.

    ``cut()`` resets every open connection of the given routes and refuses new ones (accept,
    then reset) until ``restore()``; routes not named stay up."""

    def __init__(self, routes: dict[int, int], host: str = "127.0.0.1") -> None:
        self.routes = dict(routes)
        self.host = host
        self._up = dict.fromkeys(routes, True)
        self._conns: dict[int, set[socket.socket]] = {p: set() for p in routes}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._listeners: list[socket.socket] = []

    def start(self) -> LinkProxy:
        for port in self.routes:
            ls = socket.socket()
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind((self.host, port))
            ls.listen(16)
            ls.settimeout(0.2)
            self._listeners.append(ls)
            threading.Thread(target=self._accept, args=(ls, port), daemon=True).start()
        return self

    def stop(self) -> None:
        self._closed.set()
        self.cut()
        for ls in self._listeners:
            ls.close()

    def cut(self, ports: list[int] | None = None) -> None:
        for port in self.routes if ports is None else ports:
            with self._lock:
                self._up[port] = False
                conns, self._conns[port] = self._conns[port], set()
            for s in conns:
                _reset(s)

    def restore(self, ports: list[int] | None = None) -> None:
        with self._lock:
            for port in self.routes if ports is None else ports:
                self._up[port] = True

    def _accept(self, ls: socket.socket, port: int) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = ls.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(None)
            if not self._up[port]:
                _reset(conn)
                continue
            try:
                up = socket.create_connection((self.host, self.routes[port]), timeout=2.0)
            except OSError:
                _reset(conn)
                continue
            up.settimeout(None)
            with self._lock:
                if not self._up[port]:  # cut while connecting
                    _reset(conn)
                    _reset(up)
                    continue
                self._conns[port] |= {conn, up}
            for src, dst in ((conn, up), (up, conn)):
                threading.Thread(target=self._pump, args=(src, dst), daemon=True).start()

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            _reset(src)
            _reset(dst)


def _reset(s: socket.socket) -> None:
    """Close ``s`` with an RST (like ``transport.abort()``), waking threads blocked on it."""
    with contextlib.suppress(OSError):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    with contextlib.suppress(OSError):
        s.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        s.close()


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
) -> Iterator[dict]:
    """Start sim then gateway on free ports; yield ``{"endpoint", "returncodes"}``.

    ``proxied_sts``: the gateway's upstream links run through a ``LinkProxy`` (gateway base
    port P, every slot ``P+i -> B+i`` of the sim's base port B, control slot included);
    ``info["proxy"]`` is it and ``info["sts_port"]`` the proxy port of STS-01 (slot 0), so
    ``proxy.cut([sts_port])`` drops only the gateway's link to STS-01.

    On exit both get SIGTERM; their exit codes land in ``returncodes`` (sim, gateway).
    """
    base = free_base_port()
    gw_base = base
    proxy = None
    if proxied_sts:
        gw_base = free_base_port()
        while abs(gw_base - base) < 13:
            gw_base = free_base_port()
        proxy = LinkProxy({gw_base + i: base + i for i in range(13)}).start()
    gw_port = free_port()
    sim_log = (log_dir / "sim.log").open("w")
    gw_log = (log_dir / "gateway.log").open("w")
    sim = subprocess.Popen(
        [sys.executable, "-m", "terminal_sim.serve", str(scenario), "--layout", str(ROOT / "terminal.yaml"),
         "--base-port", str(base), "--host", "127.0.0.1"],
        cwd=ROOT, stdout=sim_log, stderr=subprocess.STDOUT)
    gw = subprocess.Popen(
        [sys.executable, "-m", "terminal_gateway.serve", "--config", str(ROOT / "terminal.yaml"),
         "--port", str(gw_port), "--host", "127.0.0.1", "--upstream-host", "127.0.0.1",
         "--base-port", str(gw_base), "--n-sts", str(n_sts), "--n-agv", str(n_agv)],
        cwd=ROOT, stdout=gw_log, stderr=subprocess.STDOUT)
    endpoint = f"opc.tcp://127.0.0.1:{gw_port}"
    info = {"endpoint": endpoint, "returncodes": None, "logs": (sim_log.name, gw_log.name),
            "proxy": proxy, "sts_port": gw_base if proxy else None}
    try:
        deadline = time.monotonic() + READY_TIMEOUT_S
        n_equipment = n_sts + n_agv + len(load_layout(ROOT / "terminal.yaml").blocks)  # 1 ASC/block
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
def ecs_service(gateway_endpoint: str, *, log_dir: Path, auto_release: bool = False) -> Iterator[dict]:
    """Start ``terminal_ecs_service.serve`` on a free port (``--auto-release`` when
    ``auto_release``); yield ``{"url", "returncode", "log"}``.

    Waits for ``GET /health`` 200 and no device FAULT or OFFLINE in ``GET /equipment``; on
    exit SIGTERM and the exit code lands in ``returncode``.
    """
    port = free_port()
    log = (log_dir / "ecs.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "terminal_ecs_service.serve", "--gateway", gateway_endpoint,
         "--layout", str(ROOT / "terminal.yaml"), "--host", "127.0.0.1", "--port", str(port),
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
