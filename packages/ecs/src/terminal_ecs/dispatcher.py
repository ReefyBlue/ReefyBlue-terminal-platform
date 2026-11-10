"""Dispatching: turns work orders into equipment jobs via the EquipmentPort.

Per order (``job`` = ``current_job_id``; ``None`` means "waiting for the next
assignment in this stage", which only ``tick()`` issues):

DISCHARGE
    AWAIT_AGV(None) --TRANSPORT (agv->sts)--> AGV_EN_ROUTE(job) --done--> AT_STS(None)
    AT_STS(None) --DISCHARGE (VESSEL->sts)--> DISCHARGING(job) --done/sts_to_agv--> TO_YARD(None)
    TO_YARD(None) --TRANSPORT (sts->TP)--> TO_YARD(job) --done/agv_to_tp, AGV freed--> STACKING(None)
    STACKING(None) --STACK_IN (TP->slot)--> STACKING(job) --done--> DONE
    STACKING(job) --SLOT_OCCUPIED--> STACKING(None) with a new valid slot, or FAILED (YARD_FULL)
LOAD
    AWAIT_ASC(None) --STACK_OUT (slot->TP)--> UNSTACKING(job) --done--> AT_TP(None)
    AT_TP(None) --TRANSPORT (agv->TP)--> AGV_TO_TP(job) --done/tp_to_agv--> TO_STS(None)
    TO_STS(None) --TRANSPORT (TP->sts)--> TO_STS(job) --done--> AT_STS_LOAD(None)
    AT_STS_LOAD(None) --LOAD (sts->VESSEL)--> LOADING(job) --done/agv_to_sts, AGV freed--> DONE
    UNSTACKING(job) --SLOT_EMPTY--> FAILED (SLOT_EMPTY)
RECOVERY (any other JOB_FAILED, STATE_CHANGED->FAULT with a running job, watchdog)
    the order rolls back per ``orders.ROLLBACK`` (or keeps its stage when the box is
    on the AGV / at the TP), keeps its TOS position, ``replans += 1``; the equipment
    is excluded until STATE_CHANGED->IDLE.
    any stage --on_transfer raises--> FAILED (TRANSFER_ERROR), AGV quarantined for good.

Each STS serves its orders (DISCHARGE and LOAD together) in submit order: only the
FIFO head may use the crane; the head is also exempt from the lane lookahead.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable

from terminal_model.locations import SlotLoc, parse_location
from terminal_model.port import EquipmentEvent, EquipmentPort, EventKind
from terminal_model.types import Job, JobType, Kind, Position, State

from terminal_ecs.orders import (
    LANE_STAGES,
    NEEDS_AGV,
    PENDING,
    PRE_CRANE,
    RELEASE_AGV,
    ROLLBACK,
    TP_BUSY,
    OrderRecord,
    Stage,
    valid_slot,
)
from terminal_ecs.workorders import SlotProvider, WorkOrder, WorkOrderError

log = logging.getLogger("terminal_ecs")

_CRANE_JOB = {Stage.AT_STS: (JobType.DISCHARGE, Stage.DISCHARGING), Stage.AT_STS_LOAD: (JobType.LOAD, Stage.LOADING)}


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
        self._job_eq: dict[str, str] = {}  # running job_id -> equipment id
        self._excluded: set[str] = set()  # until STATE_CHANGED -> IDLE
        self._quarantined: set[str] = set()  # AGVs never dispatched again
        self._last_progress: dict[str, float] = {}
        self._last_pos: dict[str, Position] = {}
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
            stage = Stage.AWAIT_AGV if o.type is JobType.DISCHARGE else Stage.AWAIT_ASC
            seq = next(self._seq)
            self._open[o.container_id] = OrderRecord(o, o.yard_slot, stage, seq=seq, submit_seq=seq)
        self.tick()

    def status(self) -> dict:
        return {
            "pending": sum(r.stage in PENDING for r in self._open.values()),
            "active_jobs": len(self.jobs),
            "done": len(self.completed),
            "failed": self._failed,
            "replans": self._replans,
            "equipment": {eid: self.port.snapshot(eid).state for eid in sorted(self.port.equipment_ids())},
        }

    def on_event(self, ev: EquipmentEvent) -> None:
        if ev.equipment_id in self._agvs:
            self._last_progress[ev.equipment_id] = self.port.now()
        if ev.kind is EventKind.JOB_COMPLETED:
            self._on_completed(ev)
        elif ev.kind is EventKind.JOB_FAILED:
            self._on_failed(ev)
        elif ev.kind is EventKind.STATE_CHANGED:
            self._on_state(ev)
        self.tick()

    def tick(self) -> None:
        if self._in_tick:  # re-entered from an event raised inside assign_job / cancel_job
            self._dirty = True
            return
        self._in_tick = True
        try:
            self._watchdog()
            while True:
                self._dirty = False
                self._tick_once()
                if not self._dirty:
                    break
        finally:
            self._in_tick = False

    # -------------------------------------------------------------- tick phases

    def _tick_once(self) -> None:
        # 1. Crane: each idle STS serves only its FIFO head (DISCHARGE or LOAD).
        for sts in self._sts:
            head = self._fifo_head(sts)
            if head and head.current_job_id is None and head.stage in _CRANE_JOB and self._available(sts):
                jtype, stage = _CRANE_JOB[head.stage]
                if jtype is JobType.DISCHARGE:
                    job = Job(head.next_job_id(jtype), jtype, head.cid, "VESSEL", sts)
                else:
                    job = Job(head.next_job_id(jtype), jtype, head.cid, sts, "VESSEL")
                self._issue(sts, head, job, stage)
        # 2. Loaded AGVs (box on board): to the TP (discharge) or to the STS (load).
        for stage, src, dst in ((Stage.TO_YARD, "sts", "tp"), (Stage.TO_STS, "tp", "sts")):
            for rec in self._waiting(stage):
                if rec.agv_id is None:
                    log.error("order %s is %s without an AGV; skipping", rec.cid, stage)
                    continue
                if rec.agv_id in self._excluded:
                    continue
                locs = {"sts": rec.order.sts_id, "tp": rec.tp}
                job = Job(rec.next_job_id(JobType.TRANSPORT), JobType.TRANSPORT, rec.cid, locs[src], locs[dst])
                self._issue(rec.agv_id, rec, job, stage)
        # 3. ASCs, at most one job each per pass: STACK_IN (clears the TP) before STACK_OUT.
        used: set[str] = set()
        for rec in self._waiting(Stage.STACKING):
            asc = self._asc_by_block[rec.block]
            if asc not in used and self._available(asc):
                job = Job(rec.next_job_id(JobType.STACK_IN), JobType.STACK_IN, rec.cid, rec.tp, rec.yard_slot)
                self._issue(asc, rec, job, Stage.STACKING)
            used.add(asc)
        tp_busy = {r.block for r in self._open.values() if r.stage in TP_BUSY}
        for rec in self._waiting(Stage.AWAIT_ASC):
            asc = self._asc_by_block[rec.block]
            if asc in used or rec.block in tp_busy or not self._available(asc):
                continue
            used.add(asc)
            job = Job(rec.next_job_id(JobType.STACK_OUT), JobType.STACK_OUT, rec.cid, rec.yard_slot, rec.tp)
            if self._issue(asc, rec, job, Stage.UNSTACKING):
                tp_busy.add(rec.block)
        # 4. Empty AGVs to STS lanes / TPs, bounded by the lookahead.
        for sts in self._sts:
            self._dispatch_agvs(sts)

    def _waiting(self, stage: Stage) -> list[OrderRecord]:
        recs = [r for r in self._open.values() if r.stage is stage and r.current_job_id is None]
        return sorted(recs, key=lambda r: r.seq)

    def _fifo_head(self, sts: str) -> OrderRecord | None:
        """Earliest (submit order) order for ``sts`` that the crane has not handled yet."""
        for r in self._open.values():
            if r.order.sts_id == sts and r.stage in PRE_CRANE:
                return r
        return None

    def _dispatch_agvs(self, sts: str) -> None:
        mine = [r for r in self._open.values() if r.order.sts_id == sts]
        in_lane = sum(r.stage in LANE_STAGES for r in mine)
        head = self._fifo_head(sts)
        for rec in mine:  # submit order
            if rec.stage not in NEEDS_AGV or rec.current_job_id is not None:
                continue
            if in_lane >= self.lookahead_per_sts and rec is not head:
                return
            if not self._send_agv(rec):
                return  # no AGV available
            in_lane += 1

    def _send_agv(self, rec: OrderRecord) -> bool:
        if rec.stage is Stage.AWAIT_AGV:
            dest, target, stage = rec.order.sts_id, rec.order.sts_id, Stage.AGV_EN_ROUTE
        else:  # AT_TP; the TP has no position of its own, its ASC stands at the block
            dest, target, stage = rec.tp, self._asc_by_block[rec.block], Stage.AGV_TO_TP
        reserved = {r.agv_id for r in self._open.values() if r.agv_id is not None}
        target_pos = self.port.snapshot(target).position
        candidates = []
        for agv in self._agvs:
            if agv in reserved or not self._available(agv):
                continue
            candidates.append((self.port.snapshot(agv).position.distance_to(target_pos), agv))
        for _, agv in sorted(candidates):
            job = Job(rec.next_job_id(JobType.TRANSPORT), JobType.TRANSPORT, rec.cid, agv, dest)
            rec.agv_id = agv
            if self._issue(agv, rec, job, stage):
                return True
            rec.agv_id = None
        return False

    def _issue(self, eid: str, rec: OrderRecord, job: Job, stage: Stage) -> bool:
        # Book-keep before assign_job: the port may emit events synchronously.
        prev = (rec.stage, rec.seq)
        rec.stage, rec.seq, rec.current_job_id = stage, next(self._seq), job.job_id
        self.jobs[job.job_id] = job
        self._by_job[job.job_id] = rec
        self._job_eq[job.job_id] = eid
        if eid in self._agvs:
            self._last_progress[eid] = self.port.now()
        if self.port.assign_job(eid, job):
            rec.job_counts[job.type] = rec.job_counts.get(job.type, 0) + 1
            log.info("assign %s -> %s (%s -> %s)", job.job_id, eid, job.from_loc, job.to_loc)
            return True
        log.warning("rejected %s by %s", job.job_id, eid)
        rec.stage, rec.seq = prev
        self._detach(rec)
        return False

    def _available(self, eid: str) -> bool:
        if eid in self._excluded or eid in self._quarantined:
            return False
        return self.port.snapshot(eid).state is State.IDLE

    # ------------------------------------------------------------ event handling

    def _detach(self, rec: OrderRecord) -> None:
        """Forget the order's running job; later events for that job are ignored."""
        if rec.current_job_id is not None:
            self.jobs.pop(rec.current_job_id, None)
            self._by_job.pop(rec.current_job_id, None)
            self._job_eq.pop(rec.current_job_id, None)
            rec.current_job_id = None

    def _record_for(self, ev: EquipmentEvent) -> OrderRecord | None:
        rec = self._by_job.get(ev.job_id) if ev.job_id else None
        if rec is None:
            log.debug("ignoring %s for unknown job %s on %s", ev.kind, ev.job_id, ev.equipment_id)
            return None
        return rec

    def _enter(self, rec: OrderRecord, stage: Stage) -> None:
        rec.stage, rec.seq = stage, next(self._seq)

    def _on_completed(self, ev: EquipmentEvent) -> None:
        rec = self._record_for(ev)
        if rec is None:
            return
        self._detach(rec)
        log.info("completed %s on %s", ev.job_id, ev.equipment_id)
        agv = rec.agv_id or ev.equipment_id
        match rec.stage:
            case Stage.AGV_EN_ROUTE:
                self._enter(rec, Stage.AT_STS)
            case Stage.DISCHARGING:
                self._enter(rec, Stage.TO_YARD)
                self._transfer(rec, "sts_to_agv", ev.equipment_id, rec.agv_id)
            case Stage.TO_YARD:
                rec.agv_id = None  # AGV is free once the box is on the TP
                self._enter(rec, Stage.STACKING)
                self._transfer(rec, "agv_to_tp", agv, agv)
            case Stage.STACKING:
                self._finish(rec)
            case Stage.UNSTACKING:
                self._enter(rec, Stage.AT_TP)
            case Stage.AGV_TO_TP:
                self._enter(rec, Stage.TO_STS)
                self._transfer(rec, "tp_to_agv", agv, agv)
            case Stage.TO_STS:
                self._enter(rec, Stage.AT_STS_LOAD)
            case Stage.LOADING:
                if self._transfer(rec, "agv_to_sts", agv, agv):
                    self._finish(rec)

    def _finish(self, rec: OrderRecord) -> None:
        rec.agv_id = None
        self._enter(rec, Stage.DONE)
        del self._open[rec.cid]
        self.completed.append(rec.cid)

    def _on_failed(self, ev: EquipmentEvent) -> None:
        rec = self._record_for(ev)
        if rec is None:  # unknown, or already rolled back (FAULT state change / watchdog)
            return
        if rec.stage is Stage.STACKING and ev.reason == "SLOT_OCCUPIED":
            self._detach(rec)
            self._reslot(rec)
        elif rec.stage is Stage.UNSTACKING and ev.reason == "SLOT_EMPTY":
            self._detach(rec)
            log.error("slot %s empty for load %s; failing order", rec.yard_slot, rec.cid)
            self._fail_order(rec, "SLOT_EMPTY")
        else:
            self._replan(rec, ev.equipment_id, ev.reason or "UNKNOWN")

    def _on_state(self, ev: EquipmentEvent) -> None:
        if ev.new_state is State.IDLE:
            self._excluded.discard(ev.equipment_id)
        elif ev.new_state is State.FAULT:
            self._excluded.add(ev.equipment_id)
            for job_id, eid in list(self._job_eq.items()):
                if eid == ev.equipment_id:
                    self._replan(self._by_job[job_id], eid, "FAULT")

    def _replan(self, rec: OrderRecord, eid: str, reason: str) -> None:
        """Roll the order back to its last stable stage; it keeps its TOS position."""
        self._detach(rec)
        self._excluded.add(eid)
        self._replans += 1
        old = rec.stage
        if old in RELEASE_AGV:
            rec.agv_id = None
        rec.stage = ROLLBACK.get(old, old)
        rec.seq = rec.submit_seq
        log.warning("replan %s: %s failed on %s (%s) -> %s", rec.cid, old, eid, reason, rec.stage)

    def _reslot(self, rec: OrderRecord) -> None:
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

    def _watchdog(self) -> None:
        now = self.port.now()
        for agv in self._agvs:
            pos = self.port.snapshot(agv).position
            if self._last_pos.get(agv) != pos:
                self._last_pos[agv] = pos
                self._last_progress[agv] = now
        for job_id, eid in list(self._job_eq.items()):
            if eid not in self._agvs or now - self._last_progress.get(eid, now) <= self.watchdog_s:
                continue
            log.error("watchdog: %s made no progress on %s for %.0f s", eid, job_id, now - self._last_progress[eid])
            # Roll back first so the CANCELLED event the port may emit is ignored.
            self._replan(self._by_job[job_id], eid, "WATCHDOG")
            self.port.cancel_job(eid)
            self.port.reset(eid)

    def _fail_order(self, rec: OrderRecord, reason: str) -> None:
        rec.fail_reason = reason
        rec.agv_id = None
        self._enter(rec, Stage.FAILED)
        self._open.pop(rec.cid, None)
        self._failed += 1

    def _transfer(self, rec: OrderRecord, kind: str, eid: str, agv: str | None) -> bool:
        """Report a box hand-over; on error fail the order and quarantine the AGV that may hold it."""
        if self.on_transfer is None:
            return True
        try:
            self.on_transfer(kind, eid, rec.cid)
        except Exception:
            log.exception("on_transfer(%s, %s, %s) raised; failing order", kind, eid, rec.cid)
            if agv is not None:
                log.error("quarantining %s: it may still carry %s", agv, rec.cid)
                self._quarantined.add(agv)
            self._fail_order(rec, "TRANSFER_ERROR")
            return False
        return True
