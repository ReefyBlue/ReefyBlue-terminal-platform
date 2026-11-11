"""Dispatching: turns work orders into equipment jobs via the EquipmentPort.

Per order (``job`` = ``current_job_id``; ``None`` means "waiting for the next
assignment in this stage", which only ``tick()`` issues):

DISCHARGE
    AWAIT_AGV(None) --TRANSPORT (agv->sts)--> AGV_EN_ROUTE(job) --done--> AT_STS(None)
    AT_STS(None) --DISCHARGE (VESSEL->sts)--> DISCHARGING(job) --done/sts_to_agv--> TO_YARD(None)
    TO_YARD(None) --TRANSPORT (sts->TP)--> TO_YARD(job) --done/agv_to_tp, AGV freed--> STACKING(None)
    TO_YARD(job) --done, TP still holds another discharge box--> AWAIT_TP(None)
    AWAIT_TP(None) --TP empty: agv_to_tp, AGV freed--> STACKING(None)
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
    is excluded until STATE_CHANGED->IDLE. After a FAULT the device also needs an
    operator ``release()`` (with ``auto_release`` the IDLE releases it).
    any stage --on_transfer raises--> FAILED (TRANSFER_ERROR), AGV quarantined.
QUARANTINE
    an order that fails (any reason) while its box is on its AGV (``BOX_ON_AGV``), and
    the AGV of a failed hand-over (TRANSFER_ERROR), quarantine that AGV: it gets no job
    and needs an operator ``release()`` (cause "quarantined: box may be on board"); this
    is never lifted by ``auto_release``.
LINK LOSS (STATE_CHANGED->OFFLINE)
    no replan: the order keeps its stage and job, the device gets no new jobs and the
    watchdog clock of an offline AGV stands still. On return (reason LINK_RESTORED /
    UPSTREAM_RECONNECTED): BUSY -> the job runs on; FAULT -> as above; IDLE while the
    job had not ended -> the device's reported last job (snapshot extra ``last_job_id``
    / ``last_job_outcome``) decides: the job itself -> its JOB_COMPLETED / JOB_FAILED
    (reason FAULT_DURING_OUTAGE) is processed as if it had arrived; otherwise its outcome
    is unknown: the order waits for ``resolve_job(job, "completed"|"failed")`` and the
    device for ``release()`` (with ``auto_release`` it is resolved as failed at once,
    see ``_come_back``). ``alarms()`` lists what the operator has to do.

State sets: ``_excluded`` (until IDLE), ``_quarantined`` (until release),
``_offline`` (until the link returns) and ``_needs_release`` (until release, or IDLE
with ``auto_release`` unless quarantined) are independent; a device in any of them
gets no job (``_unusable``). A FAULT puts the device in both ``_excluded`` and
``_needs_release``; an unknown outcome or a quarantine only in ``_needs_release`` (and
``_quarantined``), so ``_needs_release`` is not a subset of ``_excluded``.

A TP holds one discharge box, from ``agv_to_tp`` until its STACK_IN completes
(``_tp_occupied``). No TO_YARD leg is issued to an occupied TP (the box waits on its
AGV); a leg already under way ends in AWAIT_TP and hands over once the TP is empty.
If the block's ASC is down while its TP is occupied, waiting orders (TO_YARD, AWAIT_TP)
are re-slotted to the first other block with a free TP and a working ASC. No STACK_OUT
is issued while a discharge box is on or heading for the TP, or a LOAD uses it
(``TP_BUSY``). A LOAD box waiting on the TP does not block a discharge: loads are
unstacked before an AGV is free, so waiting on them deadlocks.

Each STS serves its orders (DISCHARGE and LOAD together) in submit order: only the
FIFO head may use the crane; the head is also exempt from the lane lookahead, which
counts every order whose AGV is bound to the crane area (``LOOKAHEAD_STAGES``).
While the head's AGV is not in the lane yet, non-head orders keep one lane slot free
for it (at most ``lookahead_per_sts - 1`` of them lane-bound), and while a head LOAD
still needs an AGV they also leave one AGV free.

The watchdog cancels an AGV job that makes no progress for ``watchdog_s``; after
``WATCHDOG_MAX_CANCELS`` cancels of the same order it logs an ERROR, marks the order
``stuck`` (``status()["stuck"]``) and leaves it alone (the order is not failed).
Repeated failures: JOB_FAILED- and FAULT-driven replans count in the order's
``replan_streak``; a JOB_COMPLETED of one of its jobs resets it, watchdog cancels and an
operator ``resolve_job(.., "failed")`` neither count nor reset. At
``WATCHDOG_MAX_CANCELS`` in a row the order fails with reason ``REPLAN_STREAK`` (ERROR
log, ``status()["failed"]``; its TP is freed, its AGV too unless the box is on it, see
QUARANTINE) instead of being re-issued in an
endless loop of immediately failing jobs.

Finished orders (DONE and FAILED) are kept, in finish order, for ``containers_since``
and the DUPLICATE_CONTAINER check until ``prune_done(keep)`` drops the oldest beyond
``keep`` (with their ``completed`` entries and job-log entries; ``status()`` counters stay
cumulative). Only the long-running ECS service prunes; nothing here calls it, so a run
without pruning behaves exactly as before.
"""

from __future__ import annotations

import heapq
import itertools
import logging
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Literal

from terminal_model.locations import SlotLoc, parse_location
from terminal_model.port import EquipmentEvent, EquipmentPort, EventKind, LinkReason
from terminal_model.types import Job, JobType, Kind, Position, State

from terminal_ecs.alarms import Alarm, AlarmKind, ReleaseRefused
from terminal_ecs.orders import (
    BOX_ON_AGV,
    HEAD_NEEDS_AGV_LATER,
    IN_LANE,
    LOOKAHEAD_STAGES,
    NEEDS_AGV,
    PENDING,
    PRE_CRANE,
    RELEASE_AGV,
    ROLLBACK,
    TP_BUSY,
    ContainerView,
    LoggedJob,
    OrderRecord,
    Stage,
    valid_slot,
)
from terminal_ecs.workorders import SlotProvider, WorkOrder, WorkOrderError

log = logging.getLogger("terminal_ecs")

WATCHDOG_MAX_CANCELS = 5

_RETURN_REASONS = (LinkReason.LINK_RESTORED, LinkReason.UPSTREAM_RECONNECTED)
_OUTCOMES = ("completed", "failed")
_QUARANTINE_CAUSE = "quarantined: box may be on board"
_UNKNOWN_CAUSE = "unknown outcome"

_CRANE_JOB = {Stage.AT_STS: (JobType.DISCHARGE, Stage.DISCHARGING), Stage.AT_STS_LOAD: (JobType.LOAD, Stage.LOADING)}


class Dispatcher:
    def __init__(
        self,
        port: EquipmentPort,
        slot_provider: SlotProvider,
        *,
        lookahead_per_sts: int = 2,
        watchdog_s: float = 600.0,
        auto_release: bool = False,
    ) -> None:
        self.port = port
        self.slot_provider = slot_provider
        self.lookahead_per_sts = lookahead_per_sts
        self.watchdog_s = watchdog_s
        self.auto_release = auto_release
        self.on_transfer: Callable[[str, str, str], None] | None = None
        self.jobs: dict[str, Job] = {}
        self.completed: list[str] = []
        self._job_log: dict[str, LoggedJob] = {}

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
                block = port.snapshot(eid).extra.get("block_id")
                self._asc_by_block[str(block) if block else eid.split("-", 1)[1]] = eid

        self._all_eq = frozenset({*self._sts, *self._agvs, *self._asc_by_block.values()})
        self._known: set[str] = set()  # open + retained finished orders (DUPLICATE_CONTAINER)
        self._finished: dict[str, OrderRecord] = {}  # DONE/FAILED records, in finish order
        self._done_total = 0  # cumulative, unaffected by prune_done
        self._change_hw = 0  # last ContainerView seq handed out
        self._open: dict[str, OrderRecord] = {}  # submit order = per-STS FIFO
        self._by_job: dict[str, OrderRecord] = {}
        self._job_eq: dict[str, str] = {}  # running job_id -> equipment id
        self._excluded: set[str] = set()  # until STATE_CHANGED -> IDLE
        self._quarantined: set[str] = set()  # AGVs never dispatched again
        self._last_progress: dict[str, float] = {}
        self._last_pos: dict[str, Position] = {}
        self._tp_occupied: dict[str, str] = {}  # block -> container whose box is on its TP
        self._offline: set[str] = set()  # no link to the device (STATE_CHANGED -> OFFLINE)
        self._offline_job: dict[str, str] = {}  # offline device -> job it was running then
        self._needs_release: set[str] = set()  # until release() (or IDLE with auto_release)
        self._unknown: dict[str, str] = {}  # job whose outcome was lost -> equipment id
        self._release_cause: dict[str, str] = {}  # why a device needs release (first cause)
        self._faulted: set[str] = set()  # reported FAULT since its last release
        self._alarm_since: dict[tuple[AlarmKind, str], float] = {}
        self._seq = itertools.count(1)
        self._failed = 0
        self._replans = 0
        self._in_tick = False
        self._dirty = False

        for eid in sorted(self._all_eq):  # no link to it from the start
            if port.snapshot(eid).state is State.OFFLINE:
                self._go_offline(eid, "OFFLINE_AT_START")

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
            rec = OrderRecord(o, o.yard_slot, stage, seq=seq, submit_seq=seq)
            self._open[o.container_id] = rec
            self._changed(rec)
        self.tick()

    def status(self) -> dict:
        return {
            "pending": sum(r.stage in PENDING for r in self._open.values()),
            "open": len(self._open),  # every order not yet DONE or FAILED
            "active_jobs": len(self.jobs),
            "done": self._done_total,
            "failed": self._failed,
            "replans": self._replans,
            "stuck": sum(r.stuck for r in self._open.values()),
            # orders stuck with their box on an excluded/quarantined AGV
            "bound": sum(
                r.stage in (Stage.TO_YARD, Stage.TO_STS)
                and r.current_job_id is None
                and r.agv_id in self._unusable()
                for r in self._open.values()
            ),
            "equipment": {eid: self.port.snapshot(eid).state for eid in sorted(self.port.equipment_ids())},
            "offline": len(self._offline),
            "needs_release": len(self._needs_release),
            "unknown_outcomes": len(self._unknown),
        }

    def is_known(self, container_id: str) -> bool:
        """True for an open order and a finished one not yet pruned (a re-submit is a
        DUPLICATE_CONTAINER)."""
        return container_id in self._known

    def prune_done(self, keep: int) -> int:
        """Forget the oldest finished (DONE/FAILED) orders beyond the newest ``keep``: their
        records, ``completed`` entries, job-log entries and container ids (which may then
        be submitted again). Open orders and the ``status()`` counters are untouched.
        Returns how many were dropped. Never called by the dispatcher itself."""
        if keep < 0:
            raise ValueError(f"keep must be >= 0, not {keep}")
        n = len(self._finished) - keep
        if n <= 0:
            return 0
        done = 0
        for cid in list(itertools.islice(self._finished, n)):
            rec = self._finished.pop(cid)
            self._known.discard(cid)
            done += rec.stage is Stage.DONE
            for jtype, count in rec.job_counts.items():  # job ids are "<cid>-<type>-<1..count>"
                for i in range(1, count + 1):
                    self._job_log.pop(f"{cid}-{jtype}-{i}", None)
        # ``completed`` and ``_finished`` are both in finish order: the pruned DONE
        # orders are the head of ``completed``.
        del self.completed[:done]
        return n

    def containers_since(self, since: int, *, limit: int | None = None) -> tuple[int, list[ContainerView]]:
        """Orders whose open/done/failed state changed after ``since`` (``seq`` order, at
        most ``limit``), and the cursor for the next call: the last returned ``seq``, or
        with nothing to return the newest ``seq`` handed out (also when ``since`` lies
        beyond it, e.g. a cursor from another dispatcher). Pruned orders are gone; a
        cursor older than the retained window gets every retained order."""
        if since < 0:
            raise ValueError(f"since must be >= 0, not {since}")
        # Both sources are already ascending in change_seq: open orders keep their
        # submit seq, finished ones get a new seq when they enter ``_finished``.
        opened = [r for r in self._open.values() if r.change_seq > since]
        finished: list[OrderRecord] = []
        for r in reversed(self._finished.values()):
            if r.change_seq <= since:
                break
            finished.append(r)
        finished.reverse()
        recs = list(heapq.merge(opened, finished, key=lambda r: r.change_seq))
        if limit is not None:
            recs = recs[:limit]
        items = [
            ContainerView(
                seq=r.change_seq,
                container_id=r.cid,
                state="open" if r.cid in self._open else ("done" if r.stage is Stage.DONE else "failed"),
                job_type=r.order.type.value,  # type: ignore[arg-type]
                yard_slot=r.yard_slot,
            )
            for r in recs
        ]
        return (items[-1].seq if items else self._change_hw), items

    def alarms(self) -> list[Alarm]:
        """What the operator has to act on, oldest first (derived from the state sets)."""
        now = self.port.now()
        since = self._alarm_since
        out = [Alarm(AlarmKind.LINK_LOST, eid, None, since.get((AlarmKind.LINK_LOST, eid), now),
                     "no link to equipment") for eid in self._offline]
        out += [Alarm(AlarmKind.NEEDS_RELEASE, eid, None, since.get((AlarmKind.NEEDS_RELEASE, eid), now),
                      f"{self._release_cause.get(eid, 'fault')}: release required") for eid in self._needs_release]
        out += [Alarm(AlarmKind.UNKNOWN_OUTCOME, eid, job_id, since.get((AlarmKind.UNKNOWN_OUTCOME, job_id), now),
                      f"outcome of {job_id} unknown after link loss") for job_id, eid in self._unknown.items()]
        return sorted(out, key=lambda a: (a.since, a.kind, a.equipment_id, a.job_id or ""))

    def has_equipment(self, equipment_id: str) -> bool:
        return equipment_id in self._all_eq

    def unknown_jobs(self) -> frozenset[str]:
        """Jobs whose outcome was lost during a link outage and awaits ``resolve_job``."""
        return frozenset(self._unknown)

    def release(self, equipment_id: str, *, tick: bool = True) -> None:
        """Operator release after a FAULT / unknown outcome / quarantine: the device may
        get jobs again.

        ``KeyError`` for unknown equipment; ``ReleaseRefused`` while it reports FAULT or
        OFFLINE, or while one of its jobs still has an unknown outcome. These checks come
        before any change; with ``tick=False`` the caller ticks afterwards."""
        if equipment_id not in self._all_eq:
            raise KeyError(equipment_id)
        state = self.port.snapshot(equipment_id).state
        if state in (State.FAULT, State.OFFLINE):
            raise ReleaseRefused(f"equipment reports {state}")
        for job_id, eid in self._unknown.items():
            if eid == equipment_id:
                raise ReleaseRefused(f"resolve job {job_id} first")
        self._clear_release(equipment_id)
        self._excluded.discard(equipment_id)
        if equipment_id in self._quarantined:
            self._quarantined.discard(equipment_id)
            log.warning("quarantine of %s lifted by operator release", equipment_id)
        log.info("released %s", equipment_id)
        if tick:
            self.tick()

    def resolve_job(self, job_id: str, outcome: Literal["completed", "failed"], *, tick: bool = True) -> None:
        """Operator verdict on a job whose outcome was lost during a link outage.

        ``ValueError`` for another outcome; ``KeyError`` if ``job_id`` has no unknown
        outcome (both before any change). The device stays in need of release. With
        ``tick=False`` the caller ticks afterwards."""
        if outcome not in _OUTCOMES:
            raise ValueError(f"outcome must be 'completed' or 'failed', not {outcome!r}")
        if job_id not in self._unknown:
            raise KeyError(job_id)
        self._resolve(job_id, outcome)
        if tick:
            self.tick()

    @property
    def job_log(self) -> Mapping[str, LoggedJob]:
        """Read-only: every job id ever accepted by equipment -> who got it and when."""
        return MappingProxyType(self._job_log)

    def committed_agvs(self) -> frozenset[str]:
        """AGVs reserved for an open order (also while idle, e.g. waiting in a lane)."""
        return frozenset(r.agv_id for r in self._open.values() if r.agv_id is not None)

    def on_event(self, ev: EquipmentEvent) -> None:
        if ev.equipment_id in self._agvs:
            self._last_progress[ev.equipment_id] = self.port.now()
        if ev.kind in (EventKind.JOB_COMPLETED, EventKind.JOB_FAILED) and ev.job_id:
            self._outcome_seen(ev.job_id)
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
            if (
                head
                and head.current_job_id is None
                and head.stage in _CRANE_JOB
                and head.agv_id not in self._unusable()
                and self._available(sts)
            ):
                jtype, stage = _CRANE_JOB[head.stage]
                if jtype is JobType.DISCHARGE:
                    job = Job(head.next_job_id(jtype), jtype, head.cid, "VESSEL", sts)
                else:
                    job = Job(head.next_job_id(jtype), jtype, head.cid, sts, "VESSEL")
                self._issue(sts, head, job, stage)
        # 2. Loaded AGVs (box on board): hand over at a freed TP, or drive to the TP
        # (discharge) / the STS (load). A discharge box waits on its AGV while the TP is occupied.
        for rec in self._waiting(Stage.AWAIT_TP):
            if rec.block not in self._tp_occupied and rec.agv_id not in self._unusable():
                self._deliver_to_tp(rec, rec.agv_id)
            elif self._reslot_away(rec):  # ASC down: drive the box on to another block
                self._enter(rec, Stage.TO_YARD)
        for stage, src, dst in ((Stage.TO_YARD, "sts", "tp"), (Stage.TO_STS, "tp", "sts")):
            for rec in self._waiting(stage):
                if rec.agv_id is None:
                    log.error("order %s is %s without an AGV; skipping", rec.cid, stage)
                    continue
                if not self._available(rec.agv_id):  # e.g. still BUSY finishing the last leg
                    continue
                if stage is Stage.TO_YARD and rec.block in self._tp_occupied and not self._reslot_away(rec):
                    continue  # one box per TP
                locs = {"sts": rec.agv_at or rec.order.sts_id, "tp": rec.tp}
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
        claimed = self._tp_claimed(loads=True)
        for rec in self._waiting(Stage.AWAIT_ASC):
            asc = self._asc_by_block[rec.block]
            if asc in used or rec.block in claimed or not self._available(asc):
                continue
            used.add(asc)
            job = Job(rec.next_job_id(JobType.STACK_OUT), JobType.STACK_OUT, rec.cid, rec.yard_slot, rec.tp)
            if self._issue(asc, rec, job, Stage.UNSTACKING):
                claimed.add(rec.block)
        # 4. Empty AGVs to STS lanes / TPs, bounded by the lookahead.
        for sts in self._sts:
            self._dispatch_agvs(sts)

    def _tp_claimed(self, *, loads: bool) -> set[str]:
        """Blocks whose TP holds a discharge box or has one on its way or waiting at it;
        with ``loads`` also those whose TP a LOAD uses."""
        claimed = set(self._tp_occupied)
        for r in self._open.values():
            if (
                (r.stage is Stage.TO_YARD and r.current_job_id is not None)
                or r.stage is Stage.AWAIT_TP
                or (loads and r.stage in TP_BUSY)
            ):
                claimed.add(r.block)
        return claimed

    def _asc_down(self, block: str) -> bool:
        asc = self._asc_by_block[block]
        return asc in self._excluded or asc in self._needs_release or self.port.snapshot(asc).state is State.FAULT

    def _reslot_away(self, rec: OrderRecord) -> bool:
        """The TP of ``rec``'s block holds a box its faulted ASC cannot stack: move the
        order to the first other block (layout order) with an unclaimed TP and a working ASC."""
        if rec.block not in self._tp_occupied or not self._asc_down(rec.block):
            return False
        claimed = self._tp_claimed(loads=False)
        old = rec.yard_slot
        for block in self._asc_by_block:
            if block == rec.block or block in claimed or self._asc_down(block):
                continue
            new = self.slot_provider.next_slot(block)
            if new is None:
                continue
            new_block = new.split("/", 1)[0]
            if new_block not in self._asc_by_block or not valid_slot(new, new_block):
                log.error("slot provider returned invalid slot %r for %s", new, rec.cid)
                continue
            if new_block == rec.block or new_block in claimed or self._asc_down(new_block):
                continue  # provider fell back to an unusable block
            rec.yard_slot = new
            self._replans += 1
            log.info("replan %s: TP of %s blocked behind faulted ASC, slot %s -> %s", rec.cid, old.split("/")[0], old, new)
            return True
        return False

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
        in_lane = sum(r.stage in LOOKAHEAD_STAGES for r in mine)
        head = self._fifo_head(sts)
        non_head_in_lane = sum(r.stage in LOOKAHEAD_STAGES for r in mine if r is not head)
        # Until the head's AGV stands in the lane, keep a lane slot free for it: the
        # crane serves only the head, so a lane full of non-head AGVs would deadlock.
        head_outside = head is not None and head.stage not in IN_LANE
        for rec in mine:  # submit order
            if rec.stage not in NEEDS_AGV or rec.current_job_id is not None:
                continue
            if rec is not head:
                if in_lane >= self.lookahead_per_sts:
                    return
                if head_outside and non_head_in_lane >= self.lookahead_per_sts - 1:
                    return
                # A head LOAD will need an AGV later: keep one free for it, or the
                # non-head AGVs park behind it in the lane and it starves.
                head_waits = head is not None and head.agv_id is None and head.stage in HEAD_NEEDS_AGV_LATER
                if head_waits and len(self._free_agvs()) < 2:
                    return
            if not self._send_agv(rec):
                return  # no AGV available
            in_lane += 1
            non_head_in_lane += rec is not head

    def _send_agv(self, rec: OrderRecord) -> bool:
        if rec.stage is Stage.AWAIT_AGV:
            dest, target, stage = rec.order.sts_id, rec.order.sts_id, Stage.AGV_EN_ROUTE
        else:  # AT_TP; the TP has no position of its own, its ASC stands at the block
            dest, target, stage = rec.tp, self._asc_by_block[rec.block], Stage.AGV_TO_TP
        target_pos = self.port.snapshot(target).position
        candidates = [(self.port.snapshot(agv).position.distance_to(target_pos), agv) for agv in self._free_agvs()]
        for _, agv in sorted(candidates):
            job = Job(rec.next_job_id(JobType.TRANSPORT), JobType.TRANSPORT, rec.cid, agv, dest)
            rec.agv_id = agv
            if self._issue(agv, rec, job, stage):
                return True
            rec.agv_id = None
        return False

    def _free_agvs(self) -> list[str]:
        reserved = self.committed_agvs()
        return [agv for agv in self._agvs if agv not in reserved and self._available(agv)]

    def _issue(self, eid: str, rec: OrderRecord, job: Job, stage: Stage) -> bool:
        # Book-keep before assign_job: the port may emit events synchronously.
        prev = (rec.stage, rec.seq)
        rec.stage, rec.seq, rec.current_job_id = stage, next(self._seq), job.job_id
        self.jobs[job.job_id] = job
        self._by_job[job.job_id] = rec
        self._job_eq[job.job_id] = eid
        if eid in self._agvs:
            self._last_progress[eid] = self.port.now()
        self._job_log[job.job_id] = LoggedJob(job, eid, self.port.now())
        if self.port.assign_job(eid, job):
            rec.job_counts[job.type] = rec.job_counts.get(job.type, 0) + 1
            log.debug("assign %s -> %s (%s -> %s)", job.job_id, eid, job.from_loc, job.to_loc)
            return True
        log.warning("rejected %s by %s", job.job_id, eid)
        del self._job_log[job.job_id]
        rec.stage, rec.seq = prev
        self._detach(rec)
        return False

    def _available(self, eid: str) -> bool:
        if (
            eid in self._excluded
            or eid in self._quarantined
            or eid in self._offline
            or eid in self._needs_release
        ):
            return False
        return self.port.snapshot(eid).state is State.IDLE

    def _unusable(self) -> set[str]:
        """Equipment that gets no job now, whatever it reports."""
        return self._excluded | self._quarantined | self._offline | self._needs_release

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
        rec.watchdog_cancels, rec.stuck, rec.replan_streak = 0, False, 0  # the order made progress
        log.debug("completed %s on %s", ev.job_id, ev.equipment_id)
        agv = rec.agv_id or ev.equipment_id
        match rec.stage:
            case Stage.AGV_EN_ROUTE:
                self._enter(rec, Stage.AT_STS)
            case Stage.DISCHARGING:
                self._enter(rec, Stage.TO_YARD)
                self._transfer(rec, "sts_to_agv", ev.equipment_id, rec.agv_id)
            case Stage.TO_YARD:
                rec.agv_at = rec.tp
                if rec.block in self._tp_occupied:  # another discharge box still on the TP
                    self._enter(rec, Stage.AWAIT_TP)
                else:
                    self._deliver_to_tp(rec, agv)
            case Stage.STACKING:
                self._clear_tp(rec)
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

    def _deliver_to_tp(self, rec: OrderRecord, agv: str) -> None:
        rec.agv_id = rec.agv_at = None  # AGV is free once the box is on the TP
        self._tp_occupied[rec.block] = rec.cid
        self._enter(rec, Stage.STACKING)
        self._transfer(rec, "agv_to_tp", agv, agv)

    def _finish(self, rec: OrderRecord) -> None:
        rec.agv_id = None
        self._enter(rec, Stage.DONE)
        del self._open[rec.cid]
        self.completed.append(rec.cid)
        self._done_total += 1
        self._retire(rec)

    def _retire(self, rec: OrderRecord) -> None:
        """Keep a finished order for ``containers_since`` until ``prune_done``."""
        self._finished[rec.cid] = rec
        self._changed(rec)

    def _changed(self, rec: OrderRecord) -> None:
        self._change_hw += 1
        rec.change_seq = self._change_hw

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
            self._replan(rec, ev.equipment_id, ev.reason or "UNKNOWN", streak=True)

    def _on_state(self, ev: EquipmentEvent) -> None:
        eid, state = ev.equipment_id, ev.new_state
        if eid not in self._all_eq:
            log.warning("ignoring %s for unknown equipment %s", state, eid)
            return
        if state is State.OFFLINE:
            self._go_offline(eid, ev.reason)
            return
        if eid in self._offline:
            if ev.reason not in _RETURN_REASONS:
                log.warning("%s offline but reports %s without a link reason (%s); treating as return",
                            eid, state, ev.reason)
            self._come_back(eid, state)
            return
        self._apply_state(eid, state)

    def _apply_state(self, eid: str, state: State | None) -> None:
        if state is State.IDLE:
            if self.auto_release and eid not in self._quarantined:  # a quarantine needs a human
                self._clear_release(eid)
            if eid not in self._needs_release:
                self._excluded.discard(eid)
        elif state is State.FAULT:
            self._excluded.add(eid)
            self._faulted.add(eid)
            self._need_release(eid, "fault")
            for job_id, jeq in list(self._job_eq.items()):
                if jeq == eid and job_id not in self._unknown:  # unknown ones wait for resolve_job
                    self._replan(self._by_job[job_id], jeq, "FAULT", streak=True)

    def _go_offline(self, eid: str, reason: str | None) -> None:
        if eid in self._offline:
            return
        self._offline.add(eid)
        self._raise(AlarmKind.LINK_LOST, eid)
        for job_id, jeq in self._job_eq.items():
            if jeq == eid:
                self._offline_job[eid] = job_id
                break
        log.warning("%s offline (%s); job %s kept", eid, reason, self._offline_job.get(eid))

    def _come_back(self, eid: str, state: State | None) -> None:
        self._offline.discard(eid)
        self._alarm_since.pop((AlarmKind.LINK_LOST, eid), None)
        job_id = self._offline_job.pop(eid, None)
        log.info("%s back online: %s (job %s)", eid, state, job_id)
        if state is State.IDLE and job_id is not None and job_id in self._job_eq:
            # The job ended while we could not see it. The device reports the outcome of
            # its most recent job; if that is this job, process it as the event we missed
            # (a FAULT repaired during the outage shows up here as FAILED). Otherwise the
            # outcome is truly unknown: the operator decides (resolve_job); with
            # auto_release it is resolved as failed (the safe default: the order is
            # re-planned; the replan-streak cap bounds a loop).
            extra = self.port.snapshot(eid).extra
            reported = extra.get("last_job_outcome") if extra.get("last_job_id") == job_id else None
            if reported in ("COMPLETED", "FAILED"):
                log.info("reconciled %s on %s from equipment: %s", job_id, eid, reported)
                self._outcome_seen(job_id)
                if reported == "COMPLETED":
                    self._on_completed(EquipmentEvent(EventKind.JOB_COMPLETED, eid, self.port.now(), job_id=job_id))
                else:
                    self._on_failed(EquipmentEvent(EventKind.JOB_FAILED, eid, self.port.now(), job_id=job_id,
                                                   reason="FAULT_DURING_OUTAGE"))
            else:
                log.warning("outcome of %s on %s unknown after link loss", job_id, eid)
                self._unknown[job_id] = eid
                self._raise(AlarmKind.UNKNOWN_OUTCOME, job_id)
                if self.auto_release:
                    self._resolve(job_id, "failed")
                else:
                    self._need_release(eid, _UNKNOWN_CAUSE)
        self._apply_state(eid, state)

    def _resolve(self, job_id: str, outcome: str) -> None:
        eid = self._unknown.pop(job_id)
        self._alarm_since.pop((AlarmKind.UNKNOWN_OUTCOME, job_id), None)
        rec = self._by_job.get(job_id)
        log.info("resolve %s on %s: %s", job_id, eid, outcome)
        if rec is None:  # no longer running (should not happen while unknown)
            log.warning("resolve %s (%s): job no longer running on %s; verdict ignored", job_id, outcome, eid)
            return
        if outcome == "completed":
            self._on_completed(EquipmentEvent(EventKind.JOB_COMPLETED, eid, self.port.now(), job_id=job_id))
        else:
            self._replan(rec, eid, "UNKNOWN_OUTCOME")

    def _outcome_seen(self, job_id: str) -> None:
        """A JOB_COMPLETED/JOB_FAILED arrived: the job's outcome is known after all.

        If the unknown outcome was the only reason its device needed a release (no FAULT
        since, no quarantine, no other unknown job), the release is no longer needed."""
        for eid, jid in list(self._offline_job.items()):
            if jid == job_id:
                del self._offline_job[eid]
        eid = self._unknown.pop(job_id, None)
        if eid is None:
            return
        self._alarm_since.pop((AlarmKind.UNKNOWN_OUTCOME, job_id), None)
        if (
            self._release_cause.get(eid) == _UNKNOWN_CAUSE
            and eid not in self._faulted
            and eid not in self._quarantined
            and eid not in self._unknown.values()
        ):
            log.info("late outcome of %s: %s no longer needs a release", job_id, eid)
            self._clear_release(eid)

    def _raise(self, kind: AlarmKind, key: str) -> None:
        self._alarm_since.setdefault((kind, key), self.port.now())

    def _need_release(self, eid: str, cause: str) -> None:
        self._raise(AlarmKind.NEEDS_RELEASE, eid)
        self._needs_release.add(eid)
        self._release_cause.setdefault(eid, cause)

    def _clear_release(self, eid: str) -> None:
        self._needs_release.discard(eid)
        self._release_cause.pop(eid, None)
        self._faulted.discard(eid)
        self._alarm_since.pop((AlarmKind.NEEDS_RELEASE, eid), None)

    def _quarantine(self, agv: str, cid: str, why: str) -> None:
        """``agv`` may still carry ``cid``'s box: no jobs until an operator release()."""
        log.error("quarantining %s: it may still carry %s (%s)", agv, cid, why)
        self._quarantined.add(agv)
        self._need_release(agv, _QUARANTINE_CAUSE)
        self._release_cause[agv] = _QUARANTINE_CAUSE  # the most urgent cause wins

    def _replan(self, rec: OrderRecord, eid: str, reason: str, *, streak: bool = False) -> None:
        """Roll the order back to its last stable stage; it keeps its TOS position.

        ``streak``: a JOB_FAILED/FAULT-driven replan. ``WATCHDOG_MAX_CANCELS`` of them in
        a row (no completion between) fail the order (``REPLAN_STREAK``)."""
        self._detach(rec)
        self._excluded.add(eid)
        self._replans += 1
        old = rec.stage
        if old in RELEASE_AGV:
            rec.agv_id = None
        rec.stage = ROLLBACK.get(old, old)
        rec.seq = rec.submit_seq
        log.warning("replan %s: %s failed on %s (%s) -> %s", rec.cid, old, eid, reason, rec.stage)
        if streak:
            rec.replan_streak += 1
            if rec.replan_streak >= WATCHDOG_MAX_CANCELS:
                log.error("replan streak: %s failed %d times in a row on %s (%s); order failed",
                          rec.cid, rec.replan_streak, eid, reason)
                self._fail_order(rec, "REPLAN_STREAK")

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
            if eid in self._agvs and (eid in self._offline or job_id in self._unknown):
                self._last_progress[eid] = now  # the clock stands still while we cannot see it
                continue
            if eid not in self._agvs or now - self._last_progress.get(eid, now) <= self.watchdog_s:
                continue
            rec = self._by_job[job_id]
            if rec.watchdog_cancels >= WATCHDOG_MAX_CANCELS:
                if not rec.stuck:
                    rec.stuck = True
                    log.error(
                        "watchdog: %s stuck on %s after %d cancels; no longer cancelling it",
                        rec.cid, eid, rec.watchdog_cancels,
                    )
                continue
            log.error("watchdog: %s made no progress on %s for %.0f s", eid, job_id, now - self._last_progress[eid])
            rec.watchdog_cancels += 1
            # Roll back first so the CANCELLED event the port may emit is ignored.
            self._replan(rec, eid, "WATCHDOG")
            self.port.cancel_job(eid)
            self.port.reset(eid)

    def _clear_tp(self, rec: OrderRecord) -> None:
        for block, cid in list(self._tp_occupied.items()):
            if cid == rec.cid:
                del self._tp_occupied[block]

    def _fail_order(self, rec: OrderRecord, reason: str) -> None:
        # A failed order's box may physically stay on the TP; free the TP anyway so the
        # block keeps working (the failure is logged and counted). A box that may still
        # be on its AGV quarantines that AGV instead of handing it to the next order.
        if rec.stage in BOX_ON_AGV and rec.agv_id is not None and rec.agv_id not in self._quarantined:
            self._quarantine(rec.agv_id, rec.cid, reason)
        self._clear_tp(rec)
        rec.fail_reason = reason
        rec.agv_id = None
        self._enter(rec, Stage.FAILED)
        if self._open.pop(rec.cid, None) is not None:
            self._retire(rec)
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
                self._quarantine(agv, rec.cid, "TRANSFER_ERROR")
            self._fail_order(rec, "TRANSFER_ERROR")
            return False
        return True
