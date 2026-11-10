from __future__ import annotations

import asyncio
import logging

from terminal_ecs_service import events
from terminal_ecs_service.events import Broadcaster


async def test_broadcaster_drops_only_for_slow_client(caplog):
    b = Broadcaster()
    fast = b.register()
    slow = b.register()
    assert b.clients == 2
    received = []

    async def reader():
        while True:
            received.append(await fast.get())

    task = asyncio.create_task(reader())
    with caplog.at_level(logging.WARNING):
        for i in range(Broadcaster.QUEUE_SIZE + 5):
            b.publish({"type": "status", "i": i})
            await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    assert slow.qsize() == Broadcaster.QUEUE_SIZE
    assert len(received) == Broadcaster.QUEUE_SIZE + 5
    assert [m["i"] for m in received] == list(range(Broadcaster.QUEUE_SIZE + 5))
    assert sum("dropped" in r.message for r in caplog.records) == 1  # one warning


async def test_drop_warning_repeats_after_the_interval(caplog, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(events.time, "monotonic", lambda: now[0])
    b = Broadcaster()
    b.register()
    with caplog.at_level(logging.WARNING):
        for i in range(Broadcaster.QUEUE_SIZE + 3):
            b.publish({"i": i})
        now[0] += Broadcaster.WARN_INTERVAL_S
        b.publish({"i": -1})
    assert sum("dropped" in r.message for r in caplog.records) == 2


async def test_unregister_stops_delivery():
    b = Broadcaster()
    q = b.register()
    b.unregister(q)
    b.unregister(q)  # idempotent
    b.publish({"type": "status"})
    assert b.clients == 0 and q.qsize() == 0
