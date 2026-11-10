"""The ECS service core: one asyncio loop owning port, Dispatcher, KPIs and allocator.

``run()`` connects (retrying every ``CONNECT_RETRY_S`` wall seconds) and then calls
``step()`` every ``DRAIN_POLL_S``: drain the port's events (which also reach the
Dispatcher, so it dispatches from its event callback) and, on each
``SAMPLE_INTERVAL_S`` sim-time boundary from ``t0``, ``dispatcher.tick()`` +
``kpi.sample()`` followed by ``status``/``job`` messages. ``step()`` is synchronous,
so nothing else on the loop interleaves with it.

The Dispatcher and KpiCollector are created at the first successful connection (the
Dispatcher reads the fleet in its constructor) against the ``ReconnectingPort``; a
lost link unbinds and closes the connection, a later one is bound in its place and
both keep their state.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.workorders import WorkOrder, WorkOrderError
from terminal_kpi.kpi import SAMPLE_INTERVAL_S, KpiCollector
from terminal_model.port import EquipmentEvent, EquipmentPort
from terminal_tos_stub.allocator import SimpleSlotAllocator

from terminal_ecs_service.events import Broadcaster
from terminal_ecs_service.port import ReconnectingPort

__all__ = ["CONNECT_RETRY_S", "DRAIN_POLL_S", "SAMPLE_INTERVAL_S", "EcsCore", "LayoutLike"]

log = logging.getLogger(__name__)

DRAIN_POLL_S = 0.005
CONNECT_RETRY_S = 2.0

_EMPTY_STATUS = {"pending": 0, "open": 0, "active_jobs": 0, "done": 0, "failed": 0,
                 "replans": 0, "stuck": 0, "bound": 0}


class _BlockLike(Protocol):
    block: str
    bays: int
    rows: int
    tiers: int


class LayoutLike(Protocol):
    """The part of ``terminal_sim.layout.Layout`` the service needs (duck-typed: the
    service must not import the simulator)."""

    blocks: Sequence[_BlockLike]


class EcsCore:
    def __init__(
        self,
        layout: LayoutLike,
        *,
        connect: Callable[[], EquipmentPort],
        broadcaster: Broadcaster | None = None,
    ) -> None:
        self._connect = connect  # returns a connected port or raises ConnectionError
        self.port = ReconnectingPort()
        self.broadcaster = broadcaster if broadcaster is not None else Broadcaster()
        self.allocator = SimpleSlotAllocator({b.block: (b.bays, b.rows, b.tiers) for b in layout.blocks})
        self.dispatcher: Dispatcher | None = None
        self.kpi: KpiCollector | None = None
        self.t0 = 0.0
        self.next_tick = 0.0
        self.tick_count = 0
        self.started_at = time.monotonic()
        self._link: EquipmentPort | None = None  # the bound connection, closed on loss
        self._seen_jobs: set[str] = set()
        self.known_containers: set[str] = set()  # every container accepted by submit()
        self._closing: set[asyncio.Future[None]] = set()  # off-loop closes in flight
        self.port.subscribe(self._on_equipment_event)

    @property
    def connected(self) -> bool:
        return self.port.connected

    # -- connection --------------------------------------------------------------------
    async def connect_once(self) -> bool:
        """One connection attempt; ``False`` on ``ConnectionError``."""
        try:
            link = await asyncio.to_thread(self._connect)  # OPC UA connect blocks
        except ConnectionError as e:
            log.info("equipment link not available: %s", e)
            return False
        self._link = link
        try:
            self.port.bind(link)
        except ConnectionError as e:
            log.warning("equipment link lost while binding: %s", e)
            self._drop_link()
            return False
        now = self.port.now()
        if self.dispatcher is None:
            self.dispatcher = Dispatcher(self.port, self.allocator)
            self.kpi = KpiCollector(self.port, self.dispatcher)
            self.t0 = now
            self.next_tick = now + SAMPLE_INTERVAL_S
        elif self.next_tick <= now:
            # Reconnected after sim time moved on: resume at the next boundary from t0
            # instead of a burst of catch-up ticks.
            self.next_tick = self.t0 + (math.floor((now - self.t0) / SAMPLE_INTERVAL_S) + 1) * SAMPLE_INTERVAL_S
        log.info("equipment link connected at sim time %.1f", now)
        self._publish_link()
        return True

    def on_link_lost(self) -> None:
        """Unbind at once and publish ``link``; the old connection is closed off the
        loop (``OpcUaEquipmentPort.close`` blocks for up to a few seconds)."""
        log.warning("equipment link lost at sim time %.1f", self.port.now())
        self._drop_link(blocking=False)
        self._publish_link()

    def _drop_link(self, *, blocking: bool = True) -> None:
        self.port.unbind()
        link, self._link = self._link, None
        close = getattr(link, "close", None)
        if close is None:
            return
        if not blocking:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # no loop in this thread: close inline
                pass
            else:
                fut = loop.run_in_executor(None, close)
                self._closing.add(fut)
                fut.add_done_callback(self._closed)
                return
        try:
            close()
        except Exception:
            log.exception("closing the lost equipment link failed")

    def _closed(self, fut: asyncio.Future[None]) -> None:
        self._closing.discard(fut)
        if not fut.cancelled() and (exc := fut.exception()) is not None:
            log.error("closing the lost equipment link failed", exc_info=exc)

    # -- loop --------------------------------------------------------------------------
    def step(self) -> None:
        if not self.connected:
            return
        assert self.dispatcher is not None and self.kpi is not None
        try:
            self.port.drain_events()
            while self.port.now() >= self.next_tick:
                self.dispatcher.tick()
                self.kpi.sample()
                self.next_tick += SAMPLE_INTERVAL_S
                self.tick_count += 1
                self.publish_status()
                self.publish_new_jobs()
        except ConnectionError:
            self.on_link_lost()

    async def run(self) -> None:
        """Connect, then ``step()`` forever. An unexpected exception is logged and
        treated like a lost link (drop, retry after ``CONNECT_RETRY_S``) so the loop
        never dies silently; cancellation ends it and closes the connection."""
        try:
            while True:
                try:
                    if not self.connected:
                        if not await self.connect_once():
                            await asyncio.sleep(CONNECT_RETRY_S)
                            continue
                    else:
                        self.step()
                except Exception:
                    log.exception("ECS loop iteration failed; dropping the equipment link")
                    if self.connected:
                        self.on_link_lost()
                    await asyncio.sleep(CONNECT_RETRY_S)
                    continue
                await asyncio.sleep(DRAIN_POLL_S)
        finally:
            if self._link is not None:
                self._drop_link()

    # -- commands and queries -------------------------------------------------------------
    def submit(self, orders: list[WorkOrder]) -> None:
        if not self.connected or self.dispatcher is None:
            raise RuntimeError("not connected")
        for o in orders:
            self.allocator.occupy(o.yard_slot)
        try:
            self.dispatcher.submit(orders)
        except WorkOrderError:
            raise  # rejected before any order was taken
        except BaseException:
            # failed in the tick after the orders were taken (e.g. ConnectionError)
            self.known_containers.update(o.container_id for o in orders)
            raise
        self.known_containers.update(o.container_id for o in orders)
        self.dispatcher.tick()
        self.publish_new_jobs()

    def status(self) -> dict:
        base = self.dispatcher.status() if self.dispatcher is not None else {**_EMPTY_STATUS, "equipment": {}}
        return {
            **base,
            "sim_time": self.port.now(),
            "connected": self.connected,
            "uptime_s": time.monotonic() - self.started_at,
            "tick_count": self.tick_count,
        }

    def kpis(self) -> dict[str, float]:
        if self.kpi is None:
            return {}  # never connected: the API answers 503
        return self.kpi.snapshot(self.port.now() - self.t0)

    def equipment(self) -> list[dict]:
        out = []
        for eid in self.port.equipment_ids():
            s = self.port.snapshot(eid)
            out.append({
                "equipment_id": s.equipment_id,
                "kind": s.kind.value,
                "vendor": s.vendor,
                "state": s.state.value,
                "position": {"x": s.position.x, "y": s.position.y},
                "current_job_id": s.current_job_id,
                "energy_kwh": s.energy_kwh,
                "extra": dict(s.extra),
            })
        return out

    # -- event stream (spec §5) --------------------------------------------------------
    def status_message(self) -> dict:
        """The ``status`` event-stream message (spec §5); zeros before the first connection."""
        base = self.dispatcher.status() if self.dispatcher is not None else {**_EMPTY_STATUS, "equipment": {}}
        return {"type": "status", "sim_time": self.port.now(), **base}

    def publish_status(self) -> None:
        if self.dispatcher is not None:
            self.broadcaster.publish(self.status_message())

    def publish_new_jobs(self) -> None:
        if self.dispatcher is None:
            return
        for job_id, logged in self.dispatcher.job_log.items():
            if job_id in self._seen_jobs:
                continue
            self._seen_jobs.add(job_id)
            self.broadcaster.publish({
                "type": "job",
                "job_id": job_id,
                "equipment_id": logged.equipment_id,
                "sim_time": logged.assigned_at,
            })

    def _publish_link(self) -> None:
        self.broadcaster.publish({"type": "link", "connected": self.connected, "sim_time": self.port.now()})

    def _on_equipment_event(self, ev: EquipmentEvent) -> None:
        self.broadcaster.publish({
            "type": "equipment",
            "kind": ev.kind.value,
            "equipment_id": ev.equipment_id,
            "sim_time": ev.sim_time,
            "job_id": ev.job_id,
            "reason": ev.reason,
            "new_state": ev.new_state.value if ev.new_state is not None else None,
        })
