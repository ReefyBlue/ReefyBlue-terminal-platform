"""Discharge dispatching: turns work orders into equipment jobs via the EquipmentPort.

Per order the state machine is (``job`` = ``current_job_id``; ``None`` means
"waiting for the next assignment in this stage", which ``tick()`` retries):

    AWAIT_AGV(None) --TRANSPORT-1--> AGV_EN_ROUTE(job) --done--> AT_STS(None)
    AT_STS(None) --DISCHARGE-1--> DISCHARGING(job) --done/sts_to_agv--> TO_YARD(None)
    TO_YARD(None) --TRANSPORT-2--> TO_YARD(job) --done/agv_to_tp--> STACKING(None)
    STACKING(None) --STACK_IN-n--> STACKING(job) --done--> DONE
    STACKING(job) --SLOT_OCCUPIED--> STACKING(None) with a new valid slot, or FAILED (YARD_FULL)
    any stage --on_transfer raises--> FAILED (TRANSFER_ERROR)

DISCHARGE follows the per-STS submit FIFO: only the earliest not-yet-discharged
order may be discharged; later AGVs that arrive first wait in the lane.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable

from terminal_model.locations import SlotLoc, parse_location
from terminal_model.port import EquipmentEvent, EquipmentPort, EventKind
from terminal_model.types import Job, JobType, Kind, State

from terminal_ecs.orders import (
    LOOKAHEAD_STAGES,
    PRE_DISCHARGE,
    OrderRecord,
    Stage,
    valid_slot,
)
from terminal_ecs.workorders import SlotProvider, WorkOrder, WorkOrderError

log = logging.getLogger("terminal_ecs")


class Dispatcher:
    def __init__(
        self,
        port: EquipmentPort,
        slot_provider: SlotProvider,
        *,
        lookahead_per_sts: int = 2,
        watchdog_s: float = 600.0,
    ) -> None:
        self.port = port
        self.slot_provider = slot_provider
        self.lookahead_per_sts = lookahead_per_sts
        self.watchdog_s = watchdog_s
        self.on_transfer: Callable[[str, str, str], None] | None = None
        self.jobs: dict[str, Job] = {}
        self.completed: list[str] = []

        self._sts: list[str] = []
        self._agvs: list[str] = []
        self._asc_by_block: dict[str, str] = {}
        for eid in sorted(port.equipment_ids()):
            kind = port.snapshot(eid).kind
            if kind is Kind.STS:
                self._sts.append(eid)
            elif kind is Kind.AGV:
                self._agvs.append(eid)
            elif kind is Kind.ASC:
                self._asc_by_block[eid.split("-", 1)[1]] = eid

        self._known: set[str] = set()
        self._open: dict[str, OrderRecord] = {}  # submit order = per-STS FIFO
        self._by_job: dict[str, OrderRecord] = {}
        self._seq = itertools.count(1)
        self._failed = 0
        self._replans = 0
        self._in_tick = False
        self._dirty = False

        port.subscribe(self.on_event)

    # ------------------------------------------------------------------ API

    def submit(self, orders: list[WorkOrder]) -> None:
        seen: set[str] = set()
        for o in orders:
            if o.container_id in seen or o.container_id in self._known:
                raise WorkOrderError("DUPLICATE_CONTAINER", o.container_id)
            seen.add(o.container_id)
            if o.type not in (JobType.DISCHARGE, JobType.LOAD):
                raise ValueError(f"unsupported work order type: {o.type}")
            if o.sts_id not in self._sts:
                raise WorkOrderError("UNKNOWN_STS", f"{o.container_id}: {o.sts_id}")
            try:
                loc = parse_location(o.yard_slot)
            except ValueError:
                loc = None
            if not isinstance(loc, SlotLoc) or loc.block not in self._asc_by_block:
                raise WorkOrderError("INVALID_SLOT", f"{o.container_id}: {o.yard_slot}")
        for o in orders:
            self._known.add(o.container_id)
            self._open[o.container_id] = OrderRecord(o, o.yard_slot, seq=next(self._seq))
        self.tick()

    def status(self) -> dict:
        return {
            "pending": sum(r.stage is Stage.AWAIT_AGV for r in self._open.values()),
            "active_jobs": len(self.jobs),
            "done": len(self.completed),
            "failed": self._failed,
            "replans": self._replans,
            "equipment": {eid: self.port.snapshot(eid).state for eid in sorted(self.port.equipment_ids())},
        }

    def on_event(self, ev: EquipmentEvent) -> None:
        if ev.kind is EventKind.JOB_COMPLETED:
            self._on_completed(ev)
        elif ev.kind is EventKind.JOB_FAILED:
            self._on_failed(ev)
        self.tick()

    def tick(self) -> None:
        if self._in_tick:  # re-entered from an event raised inside assign_job
            self._dirty = True
            return
        self._in_tick = True
        try:
            while True:
                self._dirty = False
                self._tick_once()
                if not self._dirty:
                    break
        finally:
            self._in_tick = False

    # ------------------------------------------------------------ internals

    def _waiting(self, stage: Stage) -> list[OrderRecord]:
        recs = [r for r in self._open.values() if r.stage is stage and r.current_job_id is None]
        return sorted(recs, key=lambda r: r.seq)

    def _tick_once(self) -> None:
        # 1. Discharge in per-STS FIFO order: only the head may go, once its AGV is in the lane.
        for sts in self._sts:
            head = self._fifo_head(sts)
            if head and head.stage is Stage.AT_STS and head.current_job_id is None and self._is_idle(sts):
                job = Job(head.next_job_id(JobType.DISCHARGE), JobType.DISCHARGE, head.cid, "VESSEL", sts)
                self._issue(sts, head, job, Stage.DISCHARGING)
        # 2. Loaded AGVs to the transfer point of the target block.
        for rec in self._waiting(Stage.TO_YARD):
            if rec.agv_id is None:
                log.error("order %s is TO_YARD without an AGV; skipping", rec.cid)
                continue
            job = Job(rec.next_job_id(JobType.TRANSPORT), JobType.TRANSPORT, rec.cid, rec.order.sts_id, rec.tp)
            self._issue(rec.agv_id, rec, job, Stage.TO_YARD)
        # 3. Stack-in, FIFO per ASC by arrival at the TP.
        busy_asc: set[str] = set()
        for rec in self._waiting(Stage.STACKING):
            asc = self._asc_by_block[rec.block]
            if asc in busy_asc or not self._is_idle(asc):
                busy_asc.add(asc)
                continue
            job = Job(rec.next_job_id(JobType.STACK_IN), JobType.STACK_IN, rec.cid, rec.tp, rec.yard_slot)
            self._issue(asc, rec, job, Stage.STACKING)
            busy_asc.add(asc)  # at most one STACK_IN per ASC per pass
        # 4. Send empty AGVs to STSes, bounded by the lookahead.
        for sts in self._sts:
            self._dispatch_agvs(sts)

    def _fifo_head(self, sts: str) -> OrderRecord | None:
        """Earliest (submit order) DISCHARGE order for ``sts`` that has not started discharging."""
        for r in self._open.values():
            if r.order.sts_id == sts and r.order.type is JobType.DISCHARGE and r.stage in PRE_DISCHARGE:
                return r
        return None

    def _dispatch_agvs(self, sts: str) -> None:
        mine = [r for r in self._open.values() if r.order.sts_id == sts]
        in_lane = sum(r.stage in LOOKAHEAD_STAGES for r in mine)
        queue = [r for r in mine if r.stage is Stage.AWAIT_AGV and r.order.type is JobType.DISCHARGE]
        for rec in queue:
            if in_lane >= self.lookahead_per_sts:
                return
            if not self._send_agv(sts, rec):
                return  # no AGV available
            in_lane += 1

    def _send_agv(self, sts: str, rec: OrderRecord) -> bool:
        reserved = {r.agv_id for r in self._open.values() if r.agv_id is not None}
        sts_pos = self.port.snapshot(sts).position
        candidates = []
        for agv in self._agvs:
            if agv in reserved:
                continue
            snap = self.port.snapshot(agv)
            if snap.state is State.IDLE:
                candidates.append((snap.position.distance_to(sts_pos), agv))
        for _, agv in sorted(candidates):
            job = Job(rec.next_job_id(JobType.TRANSPORT), JobType.TRANSPORT, rec.cid, agv, sts)
            rec.agv_id = agv
            if self._issue(agv, rec, job, Stage.AGV_EN_ROUTE):
                return True
            rec.agv_id = None
        return False

    def _issue(self, eid: str, rec: OrderRecord, job: Job, stage: Stage) -> bool:
        # Book-keep before assign_job: the port may emit events synchronously.
        prev = (rec.stage, rec.seq)
        rec.stage, rec.seq, rec.current_job_id = stage, next(self._seq), job.job_id
        self.jobs[job.job_id] = job
        self._by_job[job.job_id] = rec
        if self.port.assign_job(eid, job):
            rec.job_counts[job.type] = rec.job_counts.get(job.type, 0) + 1
            log.info("assign %s -> %s (%s -> %s)", job.job_id, eid, job.from_loc, job.to_loc)
            return True
        log.warning("rejected %s by %s", job.job_id, eid)
        rec.stage, rec.seq = prev
        rec.current_job_id = None
        del self.jobs[job.job_id]
        del self._by_job[job.job_id]
        return False

    def _is_idle(self, eid: str) -> bool:
        return self.port.snapshot(eid).state is State.IDLE

    def _finish_job(self, ev: EquipmentEvent) -> OrderRecord | None:
        rec = self._by_job.pop(ev.job_id, None) if ev.job_id else None
        if rec is None:
            log.debug("ignoring %s for unknown job %s", ev.kind, ev.job_id)
            return None
        self.jobs.pop(ev.job_id, None)
        rec.current_job_id = None
        return rec

    def _enter(self, rec: OrderRecord, stage: Stage) -> None:
        rec.stage, rec.seq = stage, next(self._seq)

    def _on_completed(self, ev: EquipmentEvent) -> None:
        rec = self._finish_job(ev)
        if rec is None:
            return
        log.info("completed %s on %s", ev.job_id, ev.equipment_id)
        match rec.stage:
            case Stage.AGV_EN_ROUTE:
                self._enter(rec, Stage.AT_STS)
            case Stage.DISCHARGING:
                self._enter(rec, Stage.TO_YARD)
                self._transfer(rec, "sts_to_agv", ev.equipment_id)
            case Stage.TO_YARD:
                agv = rec.agv_id or ev.equipment_id
                rec.agv_id = None  # AGV is free once the box is on the TP
                self._enter(rec, Stage.STACKING)
                self._transfer(rec, "agv_to_tp", agv)
            case Stage.STACKING:
                self._enter(rec, Stage.DONE)
                del self._open[rec.cid]
                self.completed.append(rec.cid)

    def _on_failed(self, ev: EquipmentEvent) -> None:
        rec = self._by_job.get(ev.job_id) if ev.job_id else None
        if rec is None or not (rec.stage is Stage.STACKING and ev.reason == "SLOT_OCCUPIED"):
            # Other failures / faults are handled in Task 12; leave the order as is.
            log.warning("job %s failed on %s: %s (not handled)", ev.job_id, ev.equipment_id, ev.reason)
            return
        self._finish_job(ev)
        old = rec.yard_slot
        new = self.slot_provider.next_slot(rec.block, exclude=old)
        if new is None or not valid_slot(new, rec.block):
            if new is not None:
                log.error("slot provider returned invalid slot %r for %s in block %s", new, rec.cid, rec.block)
            log.error("yard full for %s in block %s", rec.cid, rec.block)
            self._fail_order(rec, "YARD_FULL")
            return
        log.info("replan %s: slot %s occupied -> %s", rec.cid, old, new)
        rec.yard_slot = new
        self._replans += 1

    def _fail_order(self, rec: OrderRecord, reason: str) -> None:
        rec.fail_reason = reason
        rec.agv_id = None
        self._enter(rec, Stage.FAILED)
        self._open.pop(rec.cid, None)
        self._failed += 1

    def _transfer(self, rec: OrderRecord, kind: str, eid: str) -> None:
        if self.on_transfer is None:
            return
        try:
            self.on_transfer(kind, eid, rec.cid)
        except Exception:
            log.exception("on_transfer(%s, %s, %s) raised; failing order", kind, eid, rec.cid)
            self._fail_order(rec, "TRANSFER_ERROR")
