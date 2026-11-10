from __future__ import annotations

import random
from collections.abc import Callable

import salabim

from terminal_model.port import EquipmentEvent, EquipmentSnapshot, EventKind
from terminal_model.types import Job, JobStatus, Kind, Position, State


class Equipment(salabim.Component):
    """Base for simulated equipment. Subclasses implement ``process`` as a
    Salabim generator that runs ``self.current_job``.

    The component is created as data (no process); ``assign_job`` activates the
    ``_run`` driver, which runs ``process`` once per job. A job assigned from
    inside the running process (a listener reacting synchronously to the
    JOB_COMPLETED / IDLE events) is picked up by the driver loop instead of
    re-activating the current component.
    Environments must be created with ``yieldless=False`` (generator processes).
    """

    vendor: str = "SIM"

    def __init__(
        self,
        *,
        equipment_id: str,
        kind: Kind,
        position: Position,
        rng: random.Random,
        emit: Callable[[EquipmentEvent], None],
        env: salabim.Environment,
    ) -> None:
        if getattr(env, "_yieldless", False):
            raise ValueError("Equipment requires Environment(yieldless=False)")
        super().__init__(name=equipment_id, env=env, process="")
        self.equipment_id = equipment_id
        self.kind = kind
        self.position = position
        self.rng = rng
        self._emit = emit
        self.state = State.IDLE
        self.current_job: Job | None = None
        self.energy_kwh = 0.0

    # --- job acceptance -------------------------------------------------
    def accepts(self, job: Job) -> bool:
        return True

    def assign_job(self, job: Job) -> bool:
        if self.state != State.IDLE or not self.accepts(job):
            return False
        job.status = JobStatus.RUNNING
        self.current_job = job
        self.set_state(State.BUSY)
        if self.env.current_component() is not self:
            self.activate(process="_run")
        return True

    def cancel_job(self) -> bool:
        job = self.current_job
        if job is None:
            return False
        job.status = JobStatus.FAILED
        self._emit_event(EventKind.JOB_FAILED, job_id=job.job_id, reason="CANCELLED")
        self.current_job = None
        self.cancel()  # stop the running process; state stays BUSY until reset
        return True

    def reset(self) -> bool:
        if self.state == State.FAULT:
            return False
        self.set_state(State.IDLE)
        return True

    # --- state & events -------------------------------------------------
    def set_state(self, s: State) -> None:
        if s == self.state:
            return
        self.state = s
        self._emit_event(EventKind.STATE_CHANGED, new_state=s)

    def complete_job(self) -> None:
        assert self.current_job is not None
        job = self.current_job
        job.status = JobStatus.DONE
        self._emit_event(EventKind.JOB_COMPLETED, job_id=job.job_id)
        self.current_job = None
        self.set_state(State.IDLE)

    def fail_job(self, reason: str) -> None:
        assert self.current_job is not None
        job = self.current_job
        job.status = JobStatus.FAILED
        self._emit_event(EventKind.JOB_FAILED, job_id=job.job_id, reason=reason)
        self.current_job = None
        if self.env.current_component() is not self:
            self.cancel()  # called from outside: stop the running process
        if self.state is not State.FAULT:
            self.set_state(State.IDLE)

    def _emit_event(self, kind: EventKind, **kwargs) -> None:
        self._emit(
            EquipmentEvent(
                kind=kind,
                equipment_id=self.equipment_id,
                sim_time=self.env.now(),
                **kwargs,
            )
        )

    # --- observation ----------------------------------------------------
    def extra(self) -> dict:
        return {}

    def snapshot(self) -> EquipmentSnapshot:
        return EquipmentSnapshot(
            equipment_id=self.equipment_id,
            kind=self.kind,
            vendor=self.vendor,
            state=self.state,
            position=self.position,
            current_job_id=self.current_job.job_id if self.current_job else None,
            energy_kwh=self.energy_kwh,
            extra=self.extra(),
        )

    def _run(self):
        while self.current_job is not None:
            job = self.current_job
            yield from self.process()
            if self.current_job is job:  # process returned without finishing its job
                return

    def process(self):  # pragma: no cover - abstract
        raise NotImplementedError
        yield
