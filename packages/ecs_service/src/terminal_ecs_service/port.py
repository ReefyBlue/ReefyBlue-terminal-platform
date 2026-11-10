"""A stable ``EquipmentPort`` in front of a replaceable connection.

The ``Dispatcher`` and ``KpiCollector`` subscribe once, in their constructors, and
cannot be rebound (the ECS is frozen). ``ReconnectingPort`` keeps their callbacks and
forwards events from whichever port is currently bound; on reconnection it switches
to the new port. While unbound it answers from the last-known state: ``now()`` is the
last sim time seen, ``equipment_ids()``/``snapshot()`` the last snapshots, and
commands are refused (``False`` plus a warning).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from terminal_model.port import EquipmentEvent, EquipmentPort, EquipmentSnapshot
from terminal_model.types import Job

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

    @property
    def connected(self) -> bool:
        return self._port is not None

    def bind(self, port: EquipmentPort) -> None:
        """Make ``port`` current, forward its events to every kept callback and cache
        its fleet's snapshots (may raise ``ConnectionError``; the port stays bound)."""
        self._port = port
        self._generation += 1
        # One forwarder per bind; it goes quiet at the next bind/unbind, so a stale
        # port cannot deliver and rebinding the same port does not duplicate events.
        port.subscribe(lambda ev, gen=self._generation: self._forward(gen, ev))
        self._now = port.now()
        self._ids = list(port.equipment_ids())
        for eid in self._ids:
            self._snapshots[eid] = port.snapshot(eid)

    def unbind(self) -> None:
        self._port = None
        self._generation += 1

    def _forward(self, generation: int, ev: EquipmentEvent) -> None:
        if generation != self._generation:
            return
        for cb in list(self._callbacks):
            cb(ev)

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
        return self._port.assign_job(equipment_id, job)

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
