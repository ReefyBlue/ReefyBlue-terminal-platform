"""Chaos cutter for the soak: a TCP proxy that periodically drops the link it fronts."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from terminal_soak.linkproxy import LinkProxy

log = logging.getLogger("terminal_soak.chaos")


class _Cuttable(Protocol):
    def cut(self) -> None: ...
    def restore(self) -> None: ...


def _emit(event: str) -> None:
    print(json.dumps({"event": event, "at": datetime.now(timezone.utc).isoformat()}), flush=True)


@dataclass(frozen=True)
class Schedule:
    every_s: float
    for_s: float

    def run(
        self,
        proxy: _Cuttable,
        stop: threading.Event,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_event: Callable[[str], None] = lambda _e: None,
    ) -> None:
        """Cut every ``every_s`` for ``for_s`` (restore after), repeat until ``stop`` is set.
        ``every_s == 0`` never cuts. A stop during a cut still restores the link."""
        if self.every_s <= 0:
            stop.wait()
            return
        # With the default clock/sleep, wait on the stop event so SIGTERM is prompt.
        wait = (lambda s: stop.wait(s)) if sleep is time.sleep else sleep
        next_cut = clock() + self.every_s
        while not stop.is_set():
            wait(max(0.0, next_cut - clock()))
            if stop.is_set():
                return
            proxy.cut()
            on_event("cut")
            wait(self.for_s)
            proxy.restore()
            on_event("restore")
            # Period is cut-to-cut; clamp so a paused process doesn't fire catch-up cuts.
            next_cut = max(next_cut + self.every_s, clock())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="terminal_soak.chaos")
    ap.add_argument("--listen", type=int, required=True)
    ap.add_argument("--target", required=True, help="HOST:PORT")
    ap.add_argument("--every", type=float, default=14400)
    ap.add_argument("--for", dest="for_s", type=float, default=30)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args(argv)

    target_host, _, port = args.target.rpartition(":")
    if not target_host or not port.isdigit():
        ap.error("--target must be HOST:PORT")
    # Hostname is passed through so LinkProxy resolves it on every connect (survives container IP changes).
    proxy = LinkProxy({args.listen: int(port)}, host=target_host, bind=args.host).start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        Schedule(args.every, args.for_s).run(proxy, stop, on_event=_emit)
    finally:
        proxy.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
