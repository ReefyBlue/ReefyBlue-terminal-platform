from __future__ import annotations

import threading

from terminal_soak.chaos import Schedule


class FakeClock:
    """Virtual time: ``sleep`` advances it; ``stop`` is set once it passes ``until``."""

    def __init__(self, stop: threading.Event, until: float) -> None:
        self.now = 0.0
        self.stop = stop
        self.until = until

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s
        if self.now >= self.until:
            self.stop.set()


class RecordingProxy:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.events: list[tuple[str, float]] = []

    def cut(self) -> None:
        self.events.append(("cut", self.clock.now))

    def restore(self) -> None:
        self.events.append(("restore", self.clock.now))


def test_schedule_cuts_and_restores():
    stop = threading.Event()
    fc = FakeClock(stop, until=205)
    proxy = RecordingProxy(fc)
    Schedule(every_s=100, for_s=10).run(proxy, stop, clock=fc.clock, sleep=fc.sleep)
    assert proxy.events[:3] == [("cut", 100.0), ("restore", 110.0), ("cut", 200.0)]


def test_every_zero_never_cuts():
    stop = threading.Event()
    proxy = RecordingProxy(FakeClock(stop, 0))
    threading.Timer(0.05, stop.set).start()
    Schedule(every_s=0, for_s=10).run(proxy, stop)
    assert proxy.events == []


def test_stop_during_cut_restores():
    stop = threading.Event()
    fc = FakeClock(stop, until=105)  # stop fires while the link is cut
    proxy = RecordingProxy(fc)
    Schedule(every_s=100, for_s=10).run(proxy, stop, clock=fc.clock, sleep=fc.sleep)
    assert proxy.events[-1][0] == "restore"


def test_proxy_import_path():
    import terminal_soak.linkproxy as lp
    from opcua_procs import LinkProxy

    assert LinkProxy is lp.LinkProxy


def _patched_main(monkeypatch, argv):
    import terminal_soak.chaos as chaos

    seen = {}

    class FakeProxy:
        def __init__(self, routes, host="127.0.0.1", bind=None):
            seen.update(routes=routes, host=host, bind=bind)

        def start(self):
            return self

        def stop(self):
            pass

    monkeypatch.setattr(chaos, "LinkProxy", FakeProxy)
    monkeypatch.setattr(chaos.Schedule, "run", lambda self, proxy, stop, **kw: None)
    return chaos.main(argv), seen


def test_main_passes_hostname_not_ip(monkeypatch):
    rc, seen = _patched_main(monkeypatch, ["--listen", "1234", "--target", "gateway:4840"])
    assert rc == 0
    assert seen == {"routes": {1234: 4840}, "host": "gateway", "bind": "0.0.0.0"}


def test_main_rejects_bad_target(monkeypatch):
    import pytest

    for bad in ("gateway", "gateway:", ":4840", "gateway:abc"):
        with pytest.raises(SystemExit):
            _patched_main(monkeypatch, ["--listen", "1234", "--target", bad])


def test_schedule_clamps_catch_up_after_pause():
    stop = threading.Event()
    fc = FakeClock(stop, until=10_000)
    proxy = RecordingProxy(fc)
    real_sleep = fc.sleep

    def sleep(s):
        real_sleep(s)
        if len(proxy.events) == 1 and s == 10:  # container paused for an hour during the cut
            fc.now += 3600
        if len(proxy.events) >= 6:
            stop.set()

    Schedule(every_s=100, for_s=10).run(proxy, stop, clock=fc.clock, sleep=sleep)
    cuts = [t for e, t in proxy.events if e == "cut"]
    assert cuts[1] - cuts[0] >= 3600  # no back-to-back catch-up: next cut is a full period later
    assert cuts[2] - cuts[1] >= 100


def test_linkproxy_bind_cut_restore_and_event_format(capsys):
    import json
    import socket

    import terminal_soak.chaos as chaos
    from terminal_soak.linkproxy import LinkProxy

    def free():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    up, listen = socket.socket(), free()
    up.bind(("127.0.0.1", 0))
    up.listen(4)

    def echo():
        while True:
            try:
                c, _ = up.accept()
            except OSError:
                return
            threading.Thread(target=lambda c=c: [c.sendall(d) for d in iter(lambda: c.recv(1024), b"")], daemon=True).start()

    threading.Thread(target=echo, daemon=True).start()
    proxy = LinkProxy({listen: up.getsockname()[1]}, host="localhost", bind="127.0.0.1").start()

    def roundtrip() -> bytes:
        with socket.create_connection(("127.0.0.1", listen), timeout=2) as c:
            c.sendall(b"hi")
            return c.recv(16)

    try:
        assert roundtrip() == b"hi"
        proxy.cut()
        try:
            assert roundtrip() == b""
        except OSError:
            pass
        proxy.restore()
        assert roundtrip() == b"hi"
    finally:
        proxy.stop()
        up.close()

    chaos._emit("cut")
    line = json.loads(capsys.readouterr().out.strip())
    assert line["event"] == "cut" and line["at"].endswith("+00:00")
