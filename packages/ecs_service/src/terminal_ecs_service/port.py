"""A stable ``EquipmentPort`` in front of a replaceable connection.

The ``Dispatcher`` and ``KpiCollector`` subscribe once, in their constructors, and
cannot be rebound (the ECS is frozen). ``ReconnectingPort`` keeps their callbacks and
forwards events from whichever port is currently bound; on reconnection it switches
to the new port. While unbound it answers from the last-known state: ``now()`` is the
last sim time seen, ``equipment_ids()``/``snapshot()`` the last snapshots, and
commands are refused (``False`` plus a warning). ``unbind`` announces LINK_LOST (OFFLINE)
and ``bind`` after a loss LINK_RESTORED for every device (see there); the Dispatcher
reconciles. The cached snapshots follow forwarded events and ``assign_job`` between reads.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace

from terminal_model.port import (
    EquipmentEvent,
    EquipmentPort,
    EquipmentSnapshot,
    EventKind,
    LinkReason,
)
from terminal_model.types import Job, State

log = logging.getLogger(__name__)


class ReconnectingPort:
    """``EquipmentPort`` delegating 1-to-1 to the bound port (see module docstring)."""

    def __init__(self) -> None:
        self._port: EquipmentPort | None = None
        self._callbacks: list[Callable[[EquipmentEvent], None]] = []
        self._ids: list[str] = []
        self._snapshots: dict[str, EquipmentSnapshot] = {}
        self._now = 0.0
        self._generation = 0  # bumped on every bind/unbind
        self._lost = False  # unbound after having been bound: next bind emits LINK_RESTORED
        self._racks: dict[str, dict[str, str | None]] = {}  # last rack_occupancy per block

    @property
    def connected(self) -> bool:
        return self._port is not None

    def bind(self, port: EquipmentPort) -> None:
        """Make ``port`` current, forward its events to every kept callback and cache
        its fleet's snapshots (may raise ``ConnectionError``; the port stays bound).

        The first bind emits nothing. A bind after an ``unbind`` refreshes the cache and
        then emits ``STATE_CHANGED(<fresh state>, LINK_RESTORED)`` for EVERY device, also
        unchanged ones: the Dispatcher reconciles per device (spec B2d)."""
        self._port = port
        self._generation += 1
        # One forwarder per bind; it goes quiet at the next bind/unbind, so a stale
        # port cannot deliver and rebinding the same port does not duplicate events.
        port.subscribe(lambda ev, gen=self._generation: self._forward(gen, ev))
        self._now = port.now()
        self._ids = list(port.equipment_ids())
        for eid in self._ids:
            self._snapshots[eid] = port.snapshot(eid)
        if self._lost:
            self._lost = False
            generation = self._generation
            for eid in self._ids:
                self._forward(generation, EquipmentEvent(
                    EventKind.STATE_CHANGED, eid, sim_time=self._now,
                    reason=LinkReason.LINK_RESTORED, new_state=self._snapshots[eid].state))

    def unbind(self) -> None:
        """Drop the port. Every known device gets ``STATE_CHANGED(OFFLINE, LINK_LOST)``
        and its cached state becomes OFFLINE (cache first, then the events). Unbinding
        an already unbound port does nothing."""
        if self._port is None:
            return
        self._port = None
        self._generation += 1
        self._lost = True
        generation = self._generation
        for eid in list(self._snapshots):
            self._snapshots[eid] = replace(self._snapshots[eid], state=State.OFFLINE)
        for eid in list(self._snapshots):
            self._forward(generation, EquipmentEvent(
                EventKind.STATE_CHANGED, eid, sim_time=self._now,
                reason=LinkReason.LINK_LOST, new_state=State.OFFLINE))

    def _forward(self, generation: int, ev: EquipmentEvent) -> None:
        if generation != self._generation:
            return
        self._track(ev)
        for cb in list(self._callbacks):
            cb(ev)

    def _track(self, ev: EquipmentEvent) -> None:
        """Keep the cached snapshot in step with the events (``snapshot()`` is a network
        read, so it is not called per event)."""
        snap = self._snapshots.get(ev.equipment_id)
        if snap is None:
            return
        if ev.kind is EventKind.STATE_CHANGED and ev.new_state is not None:
            self._snapshots[ev.equipment_id] = replace(snap, state=ev.new_state)
        elif (ev.kind in (EventKind.JOB_COMPLETED, EventKind.JOB_FAILED)
              and ev.job_id is not None and snap.current_job_id == ev.job_id):
            self._snapshots[ev.equipment_id] = replace(snap, current_job_id=None)

    def drain_events(self) -> int:
        """Deliver the bound port's buffered events (``OpcUaEquipmentPort``); ports that
        deliver synchronously have no ``drain_events`` and report 0."""
        drain = getattr(self._port, "drain_events", None)
        return drain() if drain is not None else 0

    # -- EquipmentPort -----------------------------------------------------------------
    def equipment_ids(self) -> list[str]:
        if self._port is not None:
            self._ids = list(self._port.equipment_ids())
        return list(self._ids)

    def snapshot(self, equipment_id: str) -> EquipmentSnapshot:
        if self._port is None:
            return self._snapshots[equipment_id]  # KeyError for an id never seen
        snap = self._port.snapshot(equipment_id)
        self._snapshots[equipment_id] = snap
        return snap

    def _refuse(self, what: str, equipment_id: str) -> bool:
        log.warning("%s %s refused: equipment link not connected", what, equipment_id)
        return False

    def assign_job(self, equipment_id: str, job: Job) -> bool:
        if self._port is None:
            return self._refuse("assign_job", equipment_id)
        ok = self._port.assign_job(equipment_id, job)
        if ok and (snap := self._snapshots.get(equipment_id)) is not None:
            self._snapshots[equipment_id] = replace(snap, current_job_id=job.job_id)
        return ok

    def cancel_job(self, equipment_id: str) -> bool:
        if self._port is None:
            return self._refuse("cancel_job", equipment_id)
        return self._port.cancel_job(equipment_id)

    def reset(self, equipment_id: str) -> bool:
        if self._port is None:
            return self._refuse("reset", equipment_id)
        return self._port.reset(equipment_id)

    def subscribe(self, callback: Callable[[EquipmentEvent], None]) -> None:
        self._callbacks.append(callback)

    def now(self) -> float:
        if self._port is not None:
            self._now = self._port.now()
        return self._now

    # -- RackOccupancyPort (v2 layouts) ------------------------------------------------
    def rack_occupancy(self, block: str) -> dict[str, str | None]:
        """The bound port's rack occupancy of ``block``; while unbound the last one read
        (``KeyError`` if never read). ``TypeError`` if the bound port has none (legacy)."""
        if self._port is not None:
            read = getattr(self._port, "rack_occupancy", None)
            if read is None:
                raise TypeError("the equipment port reports no rack occupancy")
            self._racks[block] = dict(read(block))
        return dict(self._racks[block])
