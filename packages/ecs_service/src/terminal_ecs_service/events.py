"""Fan-out of event-stream messages (spec §5) to WebSocket clients.

Each client gets its own bounded queue. ``publish`` never blocks: a full queue drops
the message for that client only, with at most one warning per client per
``WARN_INTERVAL_S``. No replay.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)


class Broadcaster:
    QUEUE_SIZE = 1000
    WARN_INTERVAL_S = 60.0

    def __init__(self) -> None:
        self._queues: dict[asyncio.Queue[dict], float] = {}  # queue -> last drop warning
        self._dropped: dict[asyncio.Queue[dict], int] = {}

    @property
    def clients(self) -> int:
        return len(self._queues)

    @property
    def client_count(self) -> int:
        """Registered clients; 0 lets publishers skip building messages nobody gets."""
        return len(self._queues)

    def register(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self._queues[q] = float("-inf")
        self._dropped[q] = 0
        return q

    def unregister(self, q: asyncio.Queue[dict]) -> None:
        self._queues.pop(q, None)
        self._dropped.pop(q, None)

    def publish(self, message: dict) -> None:
        for q in list(self._queues):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                self._dropped[q] += 1
                now = time.monotonic()
                if now - self._queues[q] >= self.WARN_INTERVAL_S:
                    self._queues[q] = now
                    log.warning(
                        "event client too slow: message dropped (%d dropped so far)", self._dropped[q]
                    )
