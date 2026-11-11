"""In-memory EquipmentPort for ECS tests (no simulator involved)."""

from __future__ import annotations

from collections.abc import Callable

from terminal_ecs.dispatcher import Dispatcher
from terminal_model.port import EquipmentEvent, EquipmentSnapshot, EventKind
from terminal_model.types import Job, Kind, Position, State


class FakePort:
    def __init__(self, equipment: dict[str, tuple[Kind, Position]]) -> None:
        self._kind = {eid: kind for eid, (kind, _) in equipment.items()}
        self._pos = {eid: pos for eid, (_, pos) in equipment.items()}
        self._state = {eid: State.IDLE for eid in equipment}
        self._job: dict[str, Job | None] = {eid: None for eid in equipment}
        self._last: dict[str, tuple[str | None, str | None]] = {eid: (None, None) for eid in equipment}
        self._extra: dict[str, dict] = {eid: {} for eid in equipment}
        self._subs: list[Callable[[EquipmentEvent], None]] = []
        self._now = 0.0
        self.calls: list[tuple[str, Job]] = []
        self.calls_cancel: list[str] = []
        self.calls_reset: list[str] = []

    # --- EquipmentPort ---
    def equipment_ids(self) -> list[str]:
        return sorted(self._kind)

    def snapshot(self, equipment_id: str) -> EquipmentSnapshot:
        job = self._job[equipment_id]
        return EquipmentSnapshot(
            equipment_id=equipment_id,
            kind=self._kind[equipment_id],
            vendor="FAKE",
            state=self._state[equipment_id],
            position=self._pos[equipment_id],
            current_job_id=job.job_id if job else None,
            energy_kwh=0.0,
            extra={**self._extra[equipment_id], "last_job_id": self._last[equipment_id][0],
                   "last_job_outcome": self._last[equipment_id][1]},
        )

    def assign_job(self, equipment_id: str, job: Job) -> bool:
        if self._state[equipment_id] is not State.IDLE:
            return False
        self.calls.append((equipment_id, job))
        self._job[equipment_id] = job
        self._state[equipment_id] = State.BUSY
        return True

    def cancel_job(self, equipment_id: str) -> bool:
        self.calls_cancel.append(equipment_id)
        return True

    def reset(self, equipment_id: str) -> bool:
        self.calls_reset.append(equipment_id)
        return True

    def subscribe(self, callback: Callable[[EquipmentEvent], None]) -> None:
        self._subs.append(callback)

    def now(self) -> float:
        return self._now

    # --- test controls ---
    def advance(self, dt: float) -> None:
        self._now += dt

    def set_position(self, equipment_id: str, pos: Position) -> None:
        self._pos[equipment_id] = pos

    def set_extra(self, equipment_id: str, **values: object) -> None:
        """Extra snapshot fields (e.g. ``loaded_container_id``); merged into ``extra``."""
        self._extra[equipment_id].update(values)

    def set_last_job(self, equipment_id: str, job_id: str | None, outcome: str | None) -> None:
        """What the device reports as its most recent ended job ("COMPLETED" | "FAILED")."""
        self._last[equipment_id] = (job_id, outcome)

    def finish_silently(self, equipment_id: str, outcome: str = "COMPLETED") -> Job:
        """The running job ends without an event (e.g. during a link outage); the device
        goes IDLE and reports it as its last job."""
        job = self._finish(equipment_id)
        self.set_last_job(equipment_id, job.job_id, outcome)
        return job

    def complete(self, equipment_id: str) -> None:
        job = self._finish(equipment_id)
        self.set_last_job(equipment_id, job.job_id, "COMPLETED")
        self._emit(EquipmentEvent(EventKind.JOB_COMPLETED, equipment_id, self._now, job_id=job.job_id))

    def fail(self, equipment_id: str, reason: str) -> None:
        job = self._finish(equipment_id)
        self.set_last_job(equipment_id, job.job_id, "FAILED")
        self._emit(
            EquipmentEvent(EventKind.JOB_FAILED, equipment_id, self._now, job_id=job.job_id, reason=reason)
        )

    def set_state(self, equipment_id: str, state: State, reason: str | None = None) -> None:
        self._state[equipment_id] = state
        self._emit(
            EquipmentEvent(EventKind.STATE_CHANGED, equipment_id, self._now, reason=reason, new_state=state)
        )

    def emit(self, ev: EquipmentEvent) -> None:
        """Deliver an arbitrary event (e.g. for equipment the port does not know)."""
        self._emit(ev)

    def _finish(self, equipment_id: str) -> Job:
        job = self._job[equipment_id]
        if job is None:
            raise AssertionError(f"{equipment_id} has no job")
        self._job[equipment_id] = None
        self._state[equipment_id] = State.IDLE
        return job

    def _emit(self, ev: EquipmentEvent) -> None:
        for cb in list(self._subs):
            cb(ev)


class FakeSlots:
    def __init__(self, slots: list[str] | None = None) -> None:
        self._slots = list(slots or [])
        self.calls: list[tuple[str, str | None]] = []

    def next_slot(self, block: str, exclude: str | None = None) -> str | None:
        self.calls.append((block, exclude))
        return self._slots.pop(0) if self._slots else None


def make_port(n_agv: int = 2) -> FakePort:
    eq: dict[str, tuple[Kind, Position]] = {
        "STS-01": (Kind.STS, Position(100, 0)),
        "ASC-A": (Kind.ASC, Position(50, 120)),
        "AGV-01": (Kind.AGV, Position(10, 50)),
        "AGV-02": (Kind.AGV, Position(300, 50)),
    }
    for i in range(3, n_agv + 1):
        eq[f"AGV-{i:02d}"] = (Kind.AGV, Position(400 + 10 * i, 50))
    return FakePort(eq)


def make_dispatcher(port: FakePort, slots: FakeSlots | None = None, **kw) -> Dispatcher:
    """Dispatcher for the pre-B2d dispatcher tests: ``auto_release=True`` (FAULT -> IDLE
    re-admits the device, truly unknown outcomes count as failed), i.e. the behaviour they pin."""
    kw.setdefault("auto_release", True)
    return Dispatcher(port, slots if slots is not None else FakeSlots(), **kw)
