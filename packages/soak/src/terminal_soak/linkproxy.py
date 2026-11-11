"""Threaded TCP proxy that can be cut and restored; shared by the e2e tests and the soak chaos cutter."""

from __future__ import annotations

import contextlib
import socket
import struct
import threading


class LinkProxy:
    """Threaded TCP proxy (``listen port -> target port`` on ``host``) whose routes can be
    cut and restored while both ends keep running; a sync twin of ``tests/gateway/tcp_proxy.py``
    for e2e tests whose main thread blocks in the runner.

    ``cut()`` resets every open connection of the given routes and refuses new ones (accept,
    then reset) until ``restore()``; routes not named stay up."""

    def __init__(self, routes: dict[int, int], host: str = "127.0.0.1", bind: str | None = None) -> None:
        self.routes = dict(routes)
        self.host = host  # upstream (target) host
        self.bind = host if bind is None else bind  # address the listeners bind to
        self._up = dict.fromkeys(routes, True)
        self._conns: dict[int, set[socket.socket]] = {p: set() for p in routes}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._listeners: list[socket.socket] = []

    def start(self) -> LinkProxy:
        for port in self.routes:
            ls = socket.socket()
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind((self.bind, port))
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
