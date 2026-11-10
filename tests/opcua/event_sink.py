"""Test helper: collects terminal OPC UA events from a client's Server node."""

from __future__ import annotations

import asyncio
from typing import Any

from terminal_model.port import EquipmentEvent
from terminal_opcua.events import (
    EventTypes,
    event_from_ua,
    resolve_event_types,
    subscribe_terminal_events,
)


class EventSink:
    """Collects (seq, EquipmentEvent) from a client's Server node; unknown events go to .unknown."""

    def __init__(self) -> None:
        self.received: list[tuple[int, EquipmentEvent]] = []
        self.unknown: list[object] = []
        self.raw: list[Any] = []  # decoded events' raw asyncua Event objects, same order
        self.types: EventTypes | None = None
        self._sub: Any = None

    async def attach(self, client: Any) -> None:
        self.types = await resolve_event_types(client)
        self._sub = await client.create_subscription(10, self)
        await subscribe_terminal_events(client, self._sub, self.types)

    async def event_notification(self, event: Any) -> None:
        assert self.types is not None
        try:
            item = event_from_ua(event, self.types)
        except ValueError:
            self.unknown.append(event)
            return
        self.received.append(item)
        self.raw.append(event)

    def events(self) -> list[EquipmentEvent]:
        return [ev for _, ev in self.received]

    async def wait_for(self, n: int, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.received) < n:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def detach(self) -> None:
        if self._sub is not None:
            await self._sub.delete()
            self._sub = None
