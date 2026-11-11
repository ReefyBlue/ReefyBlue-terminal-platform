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
