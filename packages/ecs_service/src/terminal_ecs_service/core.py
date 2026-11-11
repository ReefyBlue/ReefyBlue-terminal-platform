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

Memory stays bounded over a long run: on each sample boundary ``step()`` prunes the
Dispatcher's finished orders beyond ``known_retention`` (``Dispatcher.prune_done``).
``job`` messages come from ``Dispatcher.new_jobs_since_last_publish()`` (drained on
every publish, so nothing accumulates); no ``status`` message is built while no
event-stream client is connected.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import resource
import sys
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

__all__ = ["CONNECT_RETRY_S", "DRAIN_POLL_S", "KNOWN_RETENTION", "RECONNECT_LOG_EVERY_S", "SAMPLE_INTERVAL_S",
           "EcsCore", "LayoutLike", "rss_mb"]

log = logging.getLogger(__name__)

DRAIN_POLL_S = 0.005
CONNECT_RETRY_S = 2.0
KNOWN_RETENTION = 10_000  # finished orders kept for GET /containers and the duplicate check
RECONNECT_LOG_FIRST = 4  # failed connect attempts in a row that are all logged
RECONNECT_LOG_EVERY_S = 30.0  # after those: at most one log line per this many wall seconds
_monotonic = time.monotonic  # wall clock for the reconnect log backoff (patched in tests)

_EMPTY_STATUS = {"pending": 0, "open": 0, "active_jobs": 0, "done": 0, "failed": 0,
                 "replans": 0, "stuck": 0, "bound": 0}


def rss_mb() -> float:
    """Peak resident set size of this process in MiB (``ru_maxrss``: KiB on Linux,
    bytes on macOS)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


class _PositionLike(Protocol):
    x: float
    y: float


class _StsLike(Protocol):
    equipment_id: str
    position: _PositionLike
    lane_cells: Sequence[Sequence[int]]


class _BlockLike(Protocol):
    block: str
    asc_id: str
    tp_ws: _PositionLike
    tp_ws_cell: Sequence[int]
    bays: int
    rows: int
    tiers: int


class LayoutLike(Protocol):
    """The part of ``terminal_sim.layout.Layout`` the service needs (duck-typed: the
    service must not import the simulator)."""

    quay_length_m: float
    grid_cell_m: float
    grid_cols: int
    grid_rows: int
    sts: Sequence[_StsLike]
    blocks: Sequence[_BlockLike]
    agv_start_cells: Sequence[Sequence[int]]


class EcsCore:
    def __init__(
        self,
        layout: LayoutLike,
        *,
        connect: Callable[[], EquipmentPort],
        broadcaster: Broadcaster | None = None,
        auto_release: bool = False,
        known_retention: int = KNOWN_RETENTION,
    ) -> None:
        self.layout = layout  # static geometry, served by GET /layout
        self.auto_release = auto_release
        self.known_retention = known_retention
        self._last_alarms: dict[tuple[str, str, str | None], dict] = {}
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
        self._closing: set[asyncio.Future[None]] = set()  # off-loop closes in flight
        self._last_now: float | None = None  # last sim time seen while connected
        self._failed_attempts = 0  # consecutive failed connect attempts (reset on success)
        self._failed_since = 0.0  # wall time of the first of them
        self._failed_logged_period = 0  # last RECONNECT_LOG_EVERY_S period logged
        self.time_reversed = False  # sim time went backwards (sim restarted): /health 503
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
            self._log_unavailable(e)
            return False
        self._link = link
        try:
            self.port.bind(link)
        except ConnectionError as e:
            log.warning("equipment link lost while binding: %s", e)
            self._drop_link(blocking=False)
            return False
        self._failed_attempts = 0  # bound: the outage is over
        now = self.port.now()
        self._check_time(now)
        if self.dispatcher is None:
            self.dispatcher = Dispatcher(self.port, self.allocator, auto_release=self.auto_release)
            self.kpi = KpiCollector(self.port, self.dispatcher)
            self.t0 = now
            self.next_tick = now + SAMPLE_INTERVAL_S
        elif self.next_tick <= now:
            # Reconnected after sim time moved on: resume at the next boundary from t0
            # instead of a burst of catch-up ticks.
            self.next_tick = self.t0 + (math.floor((now - self.t0) / SAMPLE_INTERVAL_S) + 1) * SAMPLE_INTERVAL_S
        log.info("equipment link connected at sim time %.1f", now)
        self._publish_link()
        self.publish_alarms()
        return True

    def _log_unavailable(self, e: ConnectionError) -> None:
        """Log a failed attempt at info on attempts 1..``RECONNECT_LOG_FIRST`` of an
        outage, then once per ``RECONNECT_LOG_EVERY_S`` wall seconds since its first
        attempt (the others at debug). The retry timing is not affected."""
        now = _monotonic()
        self._failed_attempts += 1
        n = self._failed_attempts
        if n == 1:
            self._failed_since, self._failed_logged_period = now, 0
        period = math.floor((now - self._failed_since) / RECONNECT_LOG_EVERY_S)
        if n <= RECONNECT_LOG_FIRST or period > self._failed_logged_period:
            self._failed_logged_period = max(self._failed_logged_period, period)
            log.info("equipment link not available (attempt %d): %s", n, e)
        else:
            log.debug("equipment link not available (attempt %d): %s", n, e)

    def _check_time(self, now: float) -> None:
        """Flag (once) sim time going backwards: the sim was restarted under this ECS,
        whose Dispatcher/KPIs still hold the old run. The loop keeps running; ``/health``
        answers 503 ``sim_time_reversed`` until the service is restarted."""
        last, self._last_now = self._last_now, now
        if last is not None and now < last and not self.time_reversed:
            log.error("sim time went backwards (%.1f -> %.1f): restart the ECS service per sim run",
                      last, now)
            self.time_reversed = True

    def on_link_lost(self) -> None:
        """Unbind at once and publish ``link``; the old connection is closed off the
        loop (``OpcUaEquipmentPort.close`` blocks for up to a few seconds)."""
        log.warning("equipment link lost at sim time %.1f", self.port.now())
        self._drop_link(blocking=False)
        self._publish_link()
        self.publish_alarms()

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
            self._check_time(self.port.now())
            self.publish_alarms()  # events from drain_events() may have changed the alarm list
            while self.port.now() >= self.next_tick:
                self.dispatcher.tick()
                self.kpi.sample()
                self.next_tick += SAMPLE_INTERVAL_S
                self.tick_count += 1
                self.publish_status()
                self.publish_new_jobs()
                self._prune()  # after publishing: a pruned order's jobs were all published
                self.publish_alarms()
        except ConnectionError:
            self.on_link_lost()

    def _prune(self) -> None:
        """Bound the finished-order memory (see the module docstring)."""
        assert self.dispatcher is not None
        self.dispatcher.prune_done(self.known_retention)

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
        """Hand ``orders`` to the Dispatcher and tick. Raises ``RuntimeError`` when not
        connected and ``WorkOrderError`` when rejected, both before any order is taken.
        Once the Dispatcher has taken the orders they count as accepted: a
        ``ConnectionError`` in its ticks is handled as a lost link (``on_link_lost()``)
        and ``submit`` returns normally."""
        if not self.connected or self.dispatcher is None:
            raise RuntimeError("not connected")
        for o in orders:
            self.allocator.occupy(o.yard_slot)
        try:
            self.dispatcher.submit(orders)  # validates (no port access), takes, ticks
        except WorkOrderError:
            raise  # rejected before any order was taken
        except ConnectionError as e:
            log.warning("equipment link failed after the work orders were taken: %s", e)
            self.on_link_lost()
            self.publish_new_jobs()
            self.publish_alarms()
            return
        try:
            self.dispatcher.tick()
        except ConnectionError as e:
            log.warning("equipment link failed after the work orders were taken: %s", e)
            self.on_link_lost()
        self.publish_new_jobs()
        self.publish_alarms()

    def release(self, equipment_id: str) -> None:
        """Operator release. ``KeyError`` (unknown equipment) only from the membership
        check, and ``ReleaseRefused`` / a ``ConnectionError`` reading the device state,
        all before anything changed. Once released, a ``ConnectionError`` in the tick
        that follows is handled as a lost link (``on_link_lost()``) and ``release``
        returns normally (as ``submit``)."""
        if not self.connected or self.dispatcher is None:
            raise RuntimeError("not connected")
        d = self.dispatcher
        if not d.has_equipment(equipment_id):
            raise KeyError(equipment_id)
        try:
            d.release(equipment_id, tick=False)
        except KeyError as e:  # never a 404: membership was checked above
            raise RuntimeError(f"release({equipment_id}) failed internally: {e!r}") from e
        self._tick_after_command(f"release of {equipment_id}")

    def resolve_job(self, job_id: str, outcome: str) -> None:
        """Operator verdict on a job with unknown outcome. ``KeyError`` (no such unknown
        job) only from the membership check and ``ValueError`` (bad outcome), both before
        anything changed. Once resolved, a ``ConnectionError`` (in the verdict's
        follow-up or the tick) is handled as a lost link and ``resolve_job`` returns
        normally."""
        if not self.connected or self.dispatcher is None:
            raise RuntimeError("not connected")
        d = self.dispatcher
        if outcome not in ("completed", "failed"):
            raise ValueError(f"outcome must be 'completed' or 'failed', not {outcome!r}")
        if job_id not in d.unknown_jobs():
            raise KeyError(job_id)
        try:
            d.resolve_job(job_id, outcome, tick=False)  # type: ignore[arg-type]
        except KeyError as e:  # never a 404: membership was checked above
            raise RuntimeError(f"resolve_job({job_id}) failed internally: {e!r}") from e
        except ConnectionError as e:
            log.warning("equipment link failed after resolving %s: %s", job_id, e)
            self.on_link_lost()
            self.publish_new_jobs()
            return
        self._tick_after_command(f"resolve of {job_id}")

    def _tick_after_command(self, what: str) -> None:
        """Tick after an operator command took effect; a lost link does not undo it."""
        try:
            self.dispatcher.tick()  # type: ignore[union-attr]
        except ConnectionError as e:
            log.warning("equipment link failed after the %s: %s", what, e)
            self.on_link_lost()  # publishes link + alarms
            self.publish_new_jobs()
            return
        self.publish_new_jobs()
        self.publish_alarms()
        self.publish_status()

    def is_known(self, container_id: str) -> bool:
        """An open order or a finished one still retained has ``container_id``: a new
        work order for it is a DUPLICATE_CONTAINER. (The Dispatcher records orders before
        its first tick, so this holds even when that tick lost the link.)"""
        return self.dispatcher is not None and self.dispatcher.is_known(container_id)

    def uptime_s(self) -> float:
        return time.monotonic() - self.started_at

    def containers(self, since: int, limit: int) -> dict:
        """``GET /containers``: changes after ``since`` (see ``Dispatcher.containers_since``)."""
        if self.dispatcher is None:
            return {"next": 0, "items": []}
        nxt, items = self.dispatcher.containers_since(since, limit=limit)
        return {"next": nxt, "items": [dataclasses.asdict(v) for v in items]}

    def alarms(self) -> list[dict]:
        if self.dispatcher is None:
            return []
        return [
            {
                "kind": str(a.kind),
                "equipment_id": a.equipment_id,
                "job_id": a.job_id,
                "since_sim_time": float(a.since),
                "detail": a.detail,
            }
            for a in self.dispatcher.alarms()
        ]

    def status(self) -> dict:
        base = self.dispatcher.status() if self.dispatcher is not None else {**_EMPTY_STATUS, "equipment": {}}
        return {
            **base,
            "sim_time": self.port.now(),
            "connected": self.connected,
            "uptime_s": self.uptime_s(),
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
        if self.dispatcher is not None and self.broadcaster.client_count > 0:
            self.broadcaster.publish(self.status_message())

    def publish_new_jobs(self) -> None:
        if self.dispatcher is None:
            return
        for logged in self.dispatcher.new_jobs_since_last_publish():
            self.broadcaster.publish({
                "type": "job",
                "job_id": logged.job.job_id,
                "equipment_id": logged.equipment_id,
                "sim_time": logged.assigned_at,
            })

    def publish_alarms(self) -> None:
        """Publish ``alarm`` raised/cleared messages for the diff against the last call."""
        if self.dispatcher is None:
            return
        current = {(a["kind"], a["equipment_id"], a["job_id"]): a for a in self.alarms()}
        now = self.port.now()
        for key, a in current.items():
            if key not in self._last_alarms:
                self.broadcaster.publish({"type": "alarm", "action": "raised", **a, "sim_time": now})
        for key, a in self._last_alarms.items():
            if key not in current:
                self.broadcaster.publish({"type": "alarm", "action": "cleared", **a, "sim_time": now})
        self._last_alarms = current

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
