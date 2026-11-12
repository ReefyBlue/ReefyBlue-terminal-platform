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
    and needs an operator ``release()`` (cause "quarantined: box may be on board"). With
    ``auto_release`` an IDLE report with no box on board (snapshot extra
    ``loaded_container_id`` present and empty) lifts it like a release; with a box, or
    when the AGV does not report its load, it stays.
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
with ``auto_release`` unless quarantined with a box on board) are independent; a
device in any of them gets no job (``_unusable``). A FAULT puts the device in both ``_excluded`` and
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

RACK MODE (``layout`` is a v2 ``TerminalLayout`` with transfer zones; everything above
except the TP rules still holds). Job locations use the v2 grammar (``VESSEL:``, ``LANE:``,
``RACK:``, ``BUFFER:``, ``SLOT:<block>:<bay>:<row>`` 0-based). Per block the WS-ASC does
STACK_IN / STACK_OUT; the LS-ASC gets no job here.

DISCHARGE
    AWAIT_AGV(None) --TRANSPORT (agv->LANE, a lane is taken)--> TO_STS_LANE(job) --done--> AT_LANE(None)
    AT_LANE(None) --DISCHARGE (VESSEL->LANE)--> AT_LANE(job) --done, box on the AGV--> TO_RACK(None)
    AT_LANE(job) --done, box left in the lane buffer (AGV not IDLE)--> LANE_PICKUP(None)
    LANE_PICKUP(None) --TRANSPORT (LANE->LANE, the AGV lifts the box)--> LANE_PICKUP(job) --done--> TO_RACK(None)
    TO_RACK(None) --free rack (also in ``alternative_blocks``: re-blocked)--> TO_RACK(job), lane freed
    TO_RACK(None)/BUFFER_WAIT(None) --no free rack--> BUFFER_WAIT(job) to a buffer of its STS --done--> BUFFER_WAIT(None)
    TO_RACK(job) --done, AGV freed--> ON_RACK(None) --STACK_IN (RACK->SLOT)--> STACKING(job) --done--> DONE
LOAD
    AWAIT_ASC(None) --free rack--> STACK_OUT (SLOT->RACK) UNSTACKING(job) --done--> ON_RACK(None)
    ON_RACK(None) --TRANSPORT (agv->RACK, a lane is taken)--> AGV_TO_RACK(job) --done--> RACK_TO_STS(None)
    RACK_TO_STS(None) --TRANSPORT (RACK->LANE)--> RACK_TO_STS(job) --done--> AT_LANE(None)
    AT_LANE(None) --LOAD (LANE->VESSEL)--> AT_LANE(job) --done, AGV and lane freed--> DONE

Lanes are the lookahead: an order gets an AGV only with a free lane of its STS (no other
order holds it, no box in its lane buffer); while the FIFO head has no lane, other orders
leave one free, and while a head LOAD still needs an AGV they leave one AGV free. Racks are
claimed per order (the port's ``rack_occupancy`` adds boxes the ECS does not know); a LOAD
takes at most all but one rack of its kind in a block (of a single-rack kind: that rack),
so discharges always find one eventually. ``RACK_OCCUPIED`` is retryable: the rack is avoided and another picked
(or BUFFER_WAIT / AWAIT_ASC), without replan or exclusion. ``COLUMN_FULL`` /
``TIER_NOT_ON_TOP`` of a STACK_IN re-slots like ``SLOT_OCCUPIED``. A failed STACK_IN goes
back to ON_RACK and is re-issued to the same crane (which keeps a held box across a fault);
an ASC reporting ``holding`` another box gets no job. A crane job that fails after the
set-down (the crane reports no ``holding`` and the box is on the AGV / in the lane buffer /
on board / on the rack / off it) is not re-issued: the order carries on as if it had
completed (``_rack_handover_done``). Rack mode fails CLOSED: it needs ``rack_occupancy``
on the port, ``holding`` from STS/ASC, ``lane_buffer`` from STS and the AGV load
(``LayoutError`` at start otherwise); when at run time a failed or completed crane job
leaves the box's place unknown (a key missing), nothing is re-issued: the job's outcome is
UNKNOWN (alarm, release needed even with ``auto_release``) until ``resolve_job``.
Freed AGVs are parked on a free apron buffer (``PARK-*`` jobs: in the job log and
``_job_eq``, watched by the watchdog - a stalled park is cancelled and parked again - and
reconciled after a link loss: an unknown outcome counts as parked only if the AGV stands
on the buffer) so they never block a lane or rack point.
"""

from __future__ import annotations

import heapq
import itertools
import logging
from collections import deque
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Literal

from terminal_model.layout import LayoutError, TerminalLayout
from terminal_model.layout_grid import cell_of, grid_view
from terminal_model.locations import SlotLoc, parse_location
from terminal_model.port import (
    EquipmentEvent,
    EquipmentPort,
    EventKind,
    LinkReason,
    RackOccupancyPort,
)
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
    RACK_RELEASE_AGV,
    RACK_ROLLBACK,
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

# Rack mode: vessel cells picked when the order has no ``vessel_slot`` (spec C1 §5 vessel:
# 50 bays per berth from ``from_m + 25`` at 6.5 m, 22 rows, 10 hold + 8 deck tiers).
VESSEL_BAYS, VESSEL_BAY0_M, VESSEL_BAY_PITCH_M = 50, 25.0, 6.5
VESSEL_ROWS, VESSEL_TIERS_HOLD, VESSEL_TIERS_DECK = 22, 10, 8
_RACK_RETRY = frozenset({"RACK_OCCUPIED"})
_RESLOT_REASONS = frozenset({"COLUMN_FULL", "TIER_NOT_ON_TOP"})


class Dispatcher:
    def __init__(
        self,
        port: EquipmentPort,
        slot_provider: SlotProvider,
        *,
        lookahead_per_sts: int = 2,
        watchdog_s: float = 600.0,
        auto_release: bool = False,
        layout: TerminalLayout | None = None,
    ) -> None:
        self.port = port
        self.layout = layout
        self.rack_mode = layout is not None and bool(layout.transfer_zones)
        self.slot_provider = slot_provider
        self.lookahead_per_sts = lookahead_per_sts
        self.watchdog_s = watchdog_s
        self.auto_release = auto_release
        self.on_transfer: Callable[[str, str, str], None] | None = None
        self.jobs: dict[str, Job] = {}
        self.completed: list[str] = []
        self._job_log: dict[str, LoggedJob] = {}
        self._new_jobs: deque[LoggedJob] = deque()  # logged since the last drain

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
        self._other_asc: list[str] = []  # rack mode: the LS-ASCs (no jobs, but state events)
        self._park_jobs: dict[str, tuple[str, str]] = {}  # rack mode: PARK job id -> (AGV, buffer)
        self._park_log: list[str] = []  # ended PARK job ids still in the job log (``prune_done``)
        if self.rack_mode:
            assert layout is not None
            self._init_rack_mode(layout)

        self._all_eq = frozenset({*self._sts, *self._agvs, *self._asc_by_block.values(), *self._other_asc})
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
            if self.rack_mode:
                rec.vessel = self._vessel_for(o)
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
        for job_id in self._park_log:  # rack mode: ended PARK jobs go with them
            self._job_log.pop(job_id, None)
        self._park_log.clear()
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
        self._lift(equipment_id, "operator release")
        log.info("released %s", equipment_id)
        if tick:
            self.tick()

    def _lift(self, eid: str, by: str) -> None:
        """What a release does once it is allowed: clear the release need, the exclusion
        and a quarantine (with its alarm)."""
        self._clear_release(eid)
        self._excluded.discard(eid)
        if eid in self._quarantined:
            self._quarantined.discard(eid)
            log.warning("quarantine of %s lifted by %s", eid, by)

    def resolve_job(self, job_id: str, outcome: Literal["completed", "failed"], *, tick: bool = True) -> None:
        """Operator verdict on a job whose outcome was lost during a link outage.

        ``ValueError`` for another outcome; ``KeyError`` if ``job_id`` has no unknown
        outcome (both before any change). The device stays in need of release. With
        ``tick=False`` the caller ticks afterwards."""
        if outcome not in _OUTCOMES:
            raise ValueError(f"outcome must be 'completed' or 'failed', not {outcome!r}")
        if job_id not in self._unknown:
            raise KeyError(job_id)
        self._resolve(job_id, outcome, operator=True)
        if tick:
            self.tick()

    @property
    def job_log(self) -> Mapping[str, LoggedJob]:
        """Read-only: every job id ever accepted by equipment -> who got it and when."""
        return MappingProxyType(self._job_log)

    def new_jobs_since_last_publish(self) -> list[LoggedJob]:
        """Jobs accepted by equipment since the previous call, in log order; drains.

        A job rejected by its equipment (or pruned) before the drain is left out."""
        out = [lj for lj in self._new_jobs if self._job_log.get(lj.job.job_id) is lj]
        self._new_jobs.clear()
        return out

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
        if self.rack_mode:
            self._tick_rack()
            return
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
        logged = self._job_log[job.job_id] = LoggedJob(job, eid, self.port.now())
        self._new_jobs.append(logged)
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
        if self._park_jobs and self._park_ended(ev):
            return
        rec = self._record_for(ev)
        if rec is None:
            return
        if self.rack_mode and self._discharge_place(rec, ev.equipment_id) is None:
            self._rack_unknown(rec, ev.equipment_id, "box location unknown after the discharge")
            return
        self._detach(rec)
        rec.watchdog_cancels, rec.stuck, rec.replan_streak = 0, False, 0  # the order made progress
        log.debug("completed %s on %s", ev.job_id, ev.equipment_id)
        if self.rack_mode:
            self._rack_completed(rec, ev)
            return
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
        if self.rack_mode:
            self._release_claims(rec)
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
        if self._park_jobs and self._park_ended(ev):
            return
        rec = self._record_for(ev)
        if rec is None:  # unknown, or already rolled back (FAULT state change / watchdog)
            return
        if self.rack_mode and self._rack_failed(rec, ev):
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
            if self.auto_release:
                if eid in self._unknown.values():
                    pass  # rack mode: an unknown box location waits for resolve_job
                elif eid not in self._quarantined:
                    self._clear_release(eid)
                elif self._box_free(eid):  # nothing to recover: no human needed
                    self._lift(eid, "auto_release (IDLE, no box on board)")
            if eid not in self._needs_release:
                self._excluded.discard(eid)
        elif state is State.FAULT:
            self._excluded.add(eid)
            self._faulted.add(eid)
            self._need_release(eid, "fault")
            for job_id, jeq in list(self._job_eq.items()):
                if job_id in self._park_jobs:
                    continue  # its JOB_FAILED ends it (``_park_ended``)
                if jeq == eid and job_id not in self._unknown:  # unknown ones wait for resolve_job
                    self._replan(self._by_job[job_id], jeq, "FAULT", streak=True)

    def _box_free(self, eid: str) -> bool:
        """The device reports its load and it is empty (``None`` or ``""``); a device that
        does not report ``loaded_container_id`` may still carry a box."""
        extra = self.port.snapshot(eid).extra
        return "loaded_container_id" in extra and not extra["loaded_container_id"]

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
            elif job_id in self._park_jobs:  # a park leg needs no operator: look where the AGV is
                self._park_reconcile(eid, job_id)
            else:
                log.warning("outcome of %s on %s unknown after link loss", job_id, eid)
                self._unknown[job_id] = eid
                self._raise(AlarmKind.UNKNOWN_OUTCOME, job_id)
                if self.auto_release:
                    self._resolve(job_id, "failed")
                else:
                    self._need_release(eid, _UNKNOWN_CAUSE)
        self._apply_state(eid, state)

    def _resolve(self, job_id: str, outcome: str, *, operator: bool = False) -> None:
        """Apply a verdict on an unknown outcome. Only an ``operator`` verdict "failed" skips the
        rack-mode location check; the automatic one (``auto_release``) still runs it and, when
        the box cannot be located, the outcome stays unknown (``_rack_unknown``)."""
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
            self._replan(rec, eid, "UNKNOWN_OUTCOME", verdict=operator)

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

    def _replan(self, rec: OrderRecord, eid: str, reason: str, *, streak: bool = False,
                verdict: bool = False) -> None:
        """Roll the order back to its last stable stage; it keeps its TOS position.

        ``streak``: a JOB_FAILED/FAULT-driven replan. ``WATCHDOG_MAX_CANCELS`` of them in
        a row (no completion between) fail the order (``REPLAN_STREAK``).

        Rack mode fails closed: before a crane job is re-issued the equipment must show where
        the box is; when it cannot, the job's outcome is unknown (``_rack_unknown``). With
        ``verdict`` (an operator's ``resolve_job(.., "failed")``) the check is skipped."""
        if self.rack_mode and not verdict:
            done = self._rack_handover_done(rec, eid)
            if done is None:
                self._rack_unknown(rec, eid, reason)
                return
            if done:
                # The crane failed after it had set the box down: the step is done, not replanned.
                self._detach(rec)
                self._excluded.add(eid)
                log.warning("%s: %s failed on %s (%s) after the hand-over; carrying on",
                            rec.cid, rec.stage, eid, reason)
                rec.watchdog_cancels, rec.stuck, rec.replan_streak = 0, False, 0
                self._rack_completed(rec, EquipmentEvent(EventKind.JOB_COMPLETED, eid, self.port.now()))
                return
        self._detach(rec)
        self._excluded.add(eid)
        self._replans += 1
        old = rec.stage
        if self.rack_mode:
            self._rack_rollback(rec, old)
        else:
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
            if job_id in self._park_jobs:
                log.error("watchdog: %s made no progress on %s; parking again", eid, job_id)
                self._park_drop(job_id)  # first, so the CANCELLED event is ignored
                self.port.cancel_job(eid)
                self.port.reset(eid)
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
        on_agv = self._rack_box_on_agv(rec) if self.rack_mode else rec.stage in BOX_ON_AGV
        if on_agv and rec.agv_id is not None and rec.agv_id not in self._quarantined:
            self._quarantine(rec.agv_id, rec.cid, reason)
        if self.rack_mode:
            self._release_claims(rec)
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

    # ================================================================== rack mode

    def _init_rack_mode(self, t: TerminalLayout) -> None:
        ids = set(self.port.equipment_ids())
        self._check_rack_port(t, ids)
        self._asc_by_block = {b.id: b.asc_ws_id for b in t.blocks if b.asc_ws_id in ids}
        self._other_asc = [b.asc_ls_id for b in t.blocks if b.asc_ls_id in ids]
        self._sts_spec = {s.id: s for s in t.sts}
        self._berths = {b.id: b for b in t.quay.berths}
        self._lanes: dict[str, list[str]] = {s.id: [ln.id for ln in s.lanes] for s in t.sts}
        self._racks = {z.block: list(z.racks) for z in t.transfer_zones}
        self._rack_pos = {r.id: Position(r.x_m, r.y_m) for z in t.transfer_zones for r in z.racks}
        self._buffers = {b.id: b for b in t.apron.buffers}
        self._lane_owner: dict[str, str] = {}  # lane id -> container id
        self._rack_claim: dict[str, str] = {}  # rack id -> container id
        self._buffer_agv: dict[str, str] = {}  # buffer id -> AGV parked there / heading there
        self._vessel_moves: dict[str, int] = {}  # STS id -> vessel cells handed out
        self._park_seq = itertools.count(1)
        # AGVs start on the buffer cells of the grid view (the sim's start cells, in STS order).
        g = grid_view(t)
        order = {s.id: i for i, s in enumerate(t.sts)}
        bufs = sorted(t.apron.buffers, key=lambda b: order.get(b.sts_id, len(order)))
        by_cell = {c: b.id for b, c in zip(bufs, g.agv_start_cells, strict=False)}
        self._buffer_cell = {b: c for c, b in by_cell.items()}
        self._cell_m = g.cell_m
        for agv in self._agvs:
            p = self.port.snapshot(agv).position
            buf = by_cell.get(cell_of(p.x, p.y, g.cell_m))
            if buf is not None and buf not in self._buffer_agv:
                self._buffer_agv[buf] = agv

    def _check_rack_port(self, t: TerminalLayout, ids: set[str]) -> None:
        """Rack mode fails closed, so it needs to see where boxes are: ``rack_occupancy`` on the
        port, ``holding`` from every STS and ASC, ``lane_buffer`` from every STS and the load
        (``loaded_container_id``) from every AGV. Devices OFFLINE at start are checked when
        they matter (a missing key then makes the outcome unknown)."""
        missing: list[str] = []
        if not isinstance(self.port, RackOccupancyPort):
            missing.append("port: rack_occupancy")
        need = {Kind.STS: ("holding", "lane_buffer"), Kind.ASC: ("holding",), Kind.AGV: ("loaded_container_id",)}
        for eid in sorted(ids):
            snap = self.port.snapshot(eid)
            if snap.state is State.OFFLINE:
                continue
            missing += [f"{eid}: {k}" for k in need.get(snap.kind, ()) if k not in snap.extra]
        if missing:
            raise LayoutError("rack mode needs holding/lane_buffer/load from the equipment port; missing "
                              + ", ".join(missing))

    def _vessel_for(self, o: WorkOrder) -> str:
        """The order's vessel cell: its ``vessel_slot``, else the next cell of the STS's home bay
        (row by row, deck tiers from the bottom)."""
        if o.vessel_slot:
            return o.vessel_slot
        spec = self._sts_spec.get(o.sts_id)
        berth_id = spec.berth_ids[0] if spec and spec.berth_ids else next(iter(self._berths))
        x0 = spec.x0_m if spec else self.port.snapshot(o.sts_id).position.x
        bay = round((x0 - self._berths[berth_id].from_m - VESSEL_BAY0_M) / VESSEL_BAY_PITCH_M)
        bay = min(max(bay, 0), VESSEL_BAYS - 1)
        n = self._vessel_moves.get(o.sts_id, 0)
        self._vessel_moves[o.sts_id] = n + 1
        row = n % VESSEL_ROWS
        tier = VESSEL_TIERS_HOLD + (n // VESSEL_ROWS) % VESSEL_TIERS_DECK
        return f"VESSEL:{berth_id}:{bay}:{row}:{tier}"

    @staticmethod
    def _slot2(rec: OrderRecord) -> str:
        """``yard_slot`` (1-based ``block/bay/row/tier``) as a v2 column (0-based, no tier: the
        ASC sets down on top / picks the column's top box)."""
        loc = parse_location(rec.yard_slot)
        assert isinstance(loc, SlotLoc)
        return f"SLOT:{loc.block}:{loc.bay - 1}:{loc.row - 1}"

    # -------------------------------------------------------------- tick

    def _tick_rack(self) -> None:
        # 1. Crane: each idle STS serves only its FIFO head, once its AGV is in the lane.
        for sts in self._sts:
            head = self._rack_head(sts)
            if (
                head
                and head.current_job_id is None
                and head.stage is Stage.AT_LANE
                and head.agv_id not in self._unusable()
                and self._available(sts)
            ):
                lane = f"LANE:{head.lane}"
                assert head.vessel is not None
                jtype = head.order.type
                src, dst = (head.vessel, lane) if jtype is JobType.DISCHARGE else (lane, head.vessel)
                self._issue(sts, head, Job(head.next_job_id(jtype), jtype, head.cid, src, dst), Stage.AT_LANE)
        # 2./3. Bound AGVs and the WS-ASCs (at most one job each per pass; STACK_IN first).
        used: set[str] = set()
        self._plan_rack_discharge(used)
        self._plan_rack_load(used)
        # 4. Empty AGVs to lanes / racks, bounded by the lanes.
        for sts in self._sts:
            self._dispatch_rack_agvs(sts)
        # 5. Free AGVs not on a buffer go and park on one.
        self._park_free_agvs()

    def _rack_pre_crane(self, rec: OrderRecord) -> bool:
        if rec.order.type is JobType.LOAD:
            return True  # a LOAD is DONE when its STS job completes
        return rec.stage in (Stage.AWAIT_AGV, Stage.TO_STS_LANE, Stage.AT_LANE)

    def _rack_head(self, sts: str) -> OrderRecord | None:
        for r in self._open.values():
            if r.order.sts_id == sts and self._rack_pre_crane(r):
                return r
        return None

    def _plan_rack_discharge(self, used: set[str]) -> None:
        """Discharge boxes on their way from the lane to the stack: lane-buffer pickups, rack
        choice (or BUFFER_WAIT) and STACK_IN."""
        for rec in self._waiting(Stage.LANE_PICKUP):
            if rec.agv_id is None or not self._available(rec.agv_id):
                continue
            lane = f"LANE:{rec.lane}"
            self._issue(rec.agv_id, rec, Job(rec.next_job_id(JobType.TRANSPORT), JobType.TRANSPORT,
                                             rec.cid, lane, lane), Stage.LANE_PICKUP)
        bound = sorted([*self._waiting(Stage.TO_RACK), *self._waiting(Stage.BUFFER_WAIT)], key=lambda r: r.seq)
        for rec in bound:
            agv = rec.agv_id
            if agv is None:
                log.error("order %s is %s without an AGV; skipping", rec.cid, rec.stage)
                continue
            if not self._available(agv):
                continue
            src = rec.agv_at or (f"LANE:{rec.lane}" if rec.lane else agv)
            rack = self._discharge_rack(rec)
            if rack is not None:
                job = Job(rec.next_job_id(JobType.TRANSPORT), JobType.TRANSPORT, rec.cid, src, f"RACK:{rack}")
                rec.rack, self._rack_claim[rack] = rack, rec.cid
                if self._issue(agv, rec, job, Stage.TO_RACK):
                    self._leave_lane_or_buffer(rec)
                else:
                    self._drop_rack(rec)
                continue
            if rec.stage is Stage.BUFFER_WAIT and rec.buffer is not None:
                continue  # parked in its buffer: wait for a rack
            buf = self._free_buffer(rec.order.sts_id, self.port.snapshot(agv).position)
            if buf is None:
                continue
            job = Job(rec.next_job_id(JobType.TRANSPORT), JobType.TRANSPORT, rec.cid, src, f"BUFFER:{buf}")
            self._unpark(agv)
            rec.buffer, self._buffer_agv[buf] = buf, agv
            if self._issue(agv, rec, job, Stage.BUFFER_WAIT):
                log.info("no free rack in block %s for %s: %s waits in buffer %s", rec.block, rec.cid, agv, buf)
                self._free_lane(rec)
            else:
                self._buffer_agv.pop(buf, None)
                rec.buffer = None
        for rec in self._waiting(Stage.ON_RACK):
            if rec.order.type is not JobType.DISCHARGE:
                continue
            asc = self._asc_by_block.get(rec.block)
            if asc is None or asc in used or not self._asc_takes(asc, rec.cid):
                continue
            job = Job(rec.next_job_id(JobType.STACK_IN), JobType.STACK_IN, rec.cid, f"RACK:{rec.rack}",
                      self._slot2(rec))
            if self._issue(asc, rec, job, Stage.STACKING):
                used.add(asc)

    def _plan_rack_load(self, used: set[str]) -> None:
        """Load boxes: STACK_OUT onto a rack, and loaded AGVs from the rack to their lane."""
        for rec in self._waiting(Stage.AWAIT_ASC):
            asc = self._asc_by_block.get(rec.block)
            if asc is None or asc in used or not self._asc_takes(asc, rec.cid):
                continue
            new = rec.rack is None
            rack = rec.rack or self._load_rack(rec)
            if rack is None:
                continue  # no rack: the order stays in AWAIT_ASC
            rec.rack, self._rack_claim[rack] = rack, rec.cid
            job = Job(rec.next_job_id(JobType.STACK_OUT), JobType.STACK_OUT, rec.cid, self._slot2(rec), f"RACK:{rack}")
            if self._issue(asc, rec, job, Stage.UNSTACKING):
                used.add(asc)
            elif new:
                self._drop_rack(rec)
        for rec in self._waiting(Stage.RACK_TO_STS):
            if rec.agv_id is None or not self._available(rec.agv_id):
                continue
            src = rec.agv_at or rec.agv_id
            job = Job(rec.next_job_id(JobType.TRANSPORT), JobType.TRANSPORT, rec.cid, src, f"LANE:{rec.lane}")
            self._issue(rec.agv_id, rec, job, Stage.RACK_TO_STS)

    def _asc_takes(self, asc: str, cid: str) -> bool:
        """The crane is available and does not hold another box in its spreader."""
        if not self._available(asc):
            return False
        holding = self.port.snapshot(asc).extra.get("holding")
        return not holding or holding == cid

    def _dispatch_rack_agvs(self, sts: str) -> None:
        head = self._rack_head(sts)
        lanes = self._free_lanes(sts)
        for rec in [r for r in self._open.values() if r.order.sts_id == sts]:  # submit order
            if rec.current_job_id is not None or rec.agv_id is not None:
                continue
            needs = rec.stage is Stage.AWAIT_AGV if rec.order.type is JobType.DISCHARGE else rec.stage is Stage.ON_RACK
            if not needs:
                continue
            if not lanes:
                return
            if rec is not head and head is not None:
                if head.lane is None and len(lanes) < 2:
                    return  # keep a lane for the head: the crane serves only the head
                head_waits = (
                    head.agv_id is None and head.order.type is JobType.LOAD
                    and head.stage in (Stage.AWAIT_ASC, Stage.UNSTACKING, Stage.ON_RACK)
                )
                if head_waits and len(self._free_agvs()) < 2:
                    return
            if not self._send_rack_agv(rec, lanes[0]):
                return  # no AGV available
            lanes.pop(0)

    def _send_rack_agv(self, rec: OrderRecord, lane: str) -> bool:
        if rec.order.type is JobType.DISCHARGE:
            dest, target, stage = f"LANE:{lane}", self.port.snapshot(rec.order.sts_id).position, Stage.TO_STS_LANE
        else:
            assert rec.rack is not None
            dest, target, stage = f"RACK:{rec.rack}", self._rack_pos[rec.rack], Stage.AGV_TO_RACK
        candidates = [(self.port.snapshot(agv).position.distance_to(target), agv) for agv in self._free_agvs()]
        for _, agv in sorted(candidates):
            job = Job(rec.next_job_id(JobType.TRANSPORT), JobType.TRANSPORT, rec.cid, agv, dest)
            rec.agv_id, rec.lane, self._lane_owner[lane] = agv, lane, rec.cid
            if self._issue(agv, rec, job, stage):
                self._unpark(agv)
                rec.agv_at = None
                return True
            rec.agv_id, rec.lane = None, None
            del self._lane_owner[lane]
        return False

    # -------------------------------------------------------------- lanes, racks, buffers

    def _free_lanes(self, sts: str) -> list[str]:
        """Lanes of ``sts`` no order holds and whose lane buffer is empty."""
        buffered = self.port.snapshot(sts).extra.get("lane_buffer") or {}
        assert isinstance(buffered, Mapping)
        return [ln for ln in self._lanes.get(sts, []) if ln not in self._lane_owner and not buffered.get(ln)]

    def _free_lane(self, rec: OrderRecord) -> None:
        if rec.lane is not None and self._lane_owner.get(rec.lane) == rec.cid:
            del self._lane_owner[rec.lane]
        rec.lane = None

    def _rack_occupied(self, block: str) -> Mapping[str, object]:
        assert isinstance(self.port, RackOccupancyPort)  # checked at start
        return self.port.rack_occupancy(block)

    def _free_rack(self, block: str, reefer: bool, avoid: set[str]) -> str | None:
        """First rack of ``block``'s zone (zone order) of the box's kind that no order claims and
        the port reports empty (``Racks.free_rack`` seen from the ECS)."""
        if block not in self._racks:
            return None
        occupied = self._rack_occupied(block)
        for r in self._racks[block]:
            if r.reefer == reefer and r.id not in self._rack_claim and not occupied.get(r.id) and r.id not in avoid:
                return r.id
        return None

    def _discharge_rack(self, rec: OrderRecord) -> str | None:
        """A free rack in the order's block, else in one of its ``alternative_blocks`` (the
        order is re-blocked to a new slot there); None -> BUFFER_WAIT."""
        reefer = rec.order.reefer
        rack = self._free_rack(rec.block, reefer, rec.avoid_racks)
        if rack is not None:
            return rack
        for block in rec.order.alternative_blocks:
            if block == rec.block or block not in self._asc_by_block:
                continue
            rack = self._free_rack(block, reefer, rec.avoid_racks)
            if rack is None:
                continue
            new = self.slot_provider.next_slot(block)
            if new is None or not valid_slot(new, block):
                if new is not None:
                    log.error("slot provider returned invalid slot %r for %s in block %s", new, rec.cid, block)
                continue
            log.info("replan %s: no free rack in block %s, re-blocked %s -> %s", rec.cid, rec.block, rec.yard_slot, new)
            rec.yard_slot = new
            self._replans += 1
            return rack
        rec.avoid_racks.clear()  # avoided racks get another chance on the next tick
        return None

    def _load_rack(self, rec: OrderRecord) -> str | None:
        """A free rack for a STACK_OUT. Loads hold at most all but one rack of the box's kind in
        the block, so a discharge always finds one; a kind with a single rack (reefer) is
        shared: a load may take it (it leaves again when its AGV fetches it)."""
        reefer = rec.order.reefer
        kind = [r.id for r in self._racks.get(rec.block, []) if r.reefer == reefer]
        loads = sum(
            1 for r in self._open.values()
            if r.order.type is JobType.LOAD and r.rack in kind and r.rack is not None
        )
        if loads >= max(1, len(kind) - 1):
            return None
        rack = self._free_rack(rec.block, reefer, rec.avoid_racks)
        if rack is None:
            rec.avoid_racks.clear()
        return rack

    def _drop_rack(self, rec: OrderRecord) -> None:
        if rec.rack is not None and self._rack_claim.get(rec.rack) == rec.cid:
            del self._rack_claim[rec.rack]
        rec.rack = None

    def _free_buffer(self, sts: str | None, near: Position) -> str | None:
        """Nearest unused apron buffer, those of ``sts`` first."""
        free = [b for b in self._buffers.values() if b.id not in self._buffer_agv]
        if not free:
            return None
        return min(free, key=lambda b: (b.sts_id != sts, near.distance_to(Position(b.x_m, b.y_m)), b.id)).id

    def _unpark(self, agv: str) -> None:
        for buf, a in list(self._buffer_agv.items()):
            if a == agv:
                del self._buffer_agv[buf]

    def _leave_lane_or_buffer(self, rec: OrderRecord) -> None:
        """The order's AGV drives off (to a rack): its lane or buffer is free again."""
        self._free_lane(rec)
        if rec.buffer is not None:
            if self._buffer_agv.get(rec.buffer) == rec.agv_id:
                del self._buffer_agv[rec.buffer]
            rec.buffer = None
        rec.agv_at = None

    def _release_claims(self, rec: OrderRecord) -> None:
        self._free_lane(rec)
        self._drop_rack(rec)
        # A quarantined AGV stays where it is: its buffer stays taken (it parks there).
        if rec.buffer is not None and rec.agv_id not in self._quarantined:
            if self._buffer_agv.get(rec.buffer) == rec.agv_id:
                del self._buffer_agv[rec.buffer]
            rec.buffer = None

    def _park_free_agvs(self) -> None:
        parked = set(self._buffer_agv.values())
        for agv in self._free_agvs():
            if agv in parked:
                continue
            buf = self._free_buffer(None, self.port.snapshot(agv).position)
            if buf is None:
                return
            job = Job(f"PARK-{agv}-{next(self._park_seq)}", JobType.TRANSPORT, "", agv, f"BUFFER:{buf}")
            # Book-keep before assign_job (events may come synchronously), like ``_issue``.
            self._buffer_agv[buf] = agv
            self._park_jobs[job.job_id] = (agv, buf)
            self._job_eq[job.job_id] = agv
            self._last_progress[agv] = self.port.now()
            logged = self._job_log[job.job_id] = LoggedJob(job, agv, self.port.now())
            self._new_jobs.append(logged)
            if not self.port.assign_job(agv, job):
                del self._job_log[job.job_id]
                self._park_drop(job.job_id)

    def _park_drop(self, job_id: str) -> None:
        """Forget a PARK job; its AGV counts as not parked (parked again on a later tick)."""
        agv, buf = self._park_jobs.pop(job_id)
        self._job_eq.pop(job_id, None)
        self._park_log.append(job_id)
        if self._buffer_agv.get(buf) == agv:
            del self._buffer_agv[buf]

    def _park_ended(self, ev: EquipmentEvent) -> bool:
        """A PARK job ended (True); a failed one leaves its AGV unparked (parked again later)."""
        if not ev.job_id or ev.job_id not in self._park_jobs:
            return False
        if ev.kind is EventKind.JOB_FAILED:
            self._park_drop(ev.job_id)
        else:
            self._park_jobs.pop(ev.job_id)
            self._job_eq.pop(ev.job_id, None)
            self._park_log.append(ev.job_id)
        return True

    def _at_buffer(self, agv: str, buf: str) -> bool:
        p = self.port.snapshot(agv).position
        return cell_of(p.x, p.y, self._cell_m) == self._buffer_cell.get(buf)

    def _park_reconcile(self, agv: str, job_id: str) -> None:
        """A PARK leg ended during a link outage with an unknown outcome: the AGV is parked
        only if it actually stands on the buffer."""
        _, buf = self._park_jobs[job_id]
        if self._at_buffer(agv, buf):
            log.info("park %s of %s ended during the outage: on buffer %s", job_id, agv, buf)
            self._park_jobs.pop(job_id)
            self._job_eq.pop(job_id, None)
            self._park_log.append(job_id)
        else:
            log.info("park %s of %s ended during the outage off buffer %s: parking again", job_id, agv, buf)
            self._park_drop(job_id)

    # -------------------------------------------------------------- events

    def _carrying(self, agv: str | None, cid: str) -> bool | None:
        """Whether the AGV reports ``cid`` on board; None (unknown) without an AGV or when it
        does not report its load. Callers treat None as an unknown box location."""
        if agv is None:
            return None
        extra = self.port.snapshot(agv).extra
        if "loaded_container_id" not in extra:
            return None
        return cid in (extra.get("loaded_container_id"), extra.get("load_a"), extra.get("load_b"))

    def _discharge_place(self, rec: OrderRecord, sts: str) -> Literal["agv", "buffer", "n/a"] | None:
        """Where a completed DISCHARGE (AT_LANE) put its box: "agv" or "buffer" (one of the two,
        seen from the AGV load or the STS ``lane_buffer``), None when neither shows it or they
        contradict each other; "n/a" for any other order, stage or equipment."""
        if not (rec.stage is Stage.AT_LANE and rec.order.type is JobType.DISCHARGE and sts == rec.order.sts_id):
            return "n/a"
        carrying = self._carrying(rec.agv_id, rec.cid)
        buffered = self.port.snapshot(sts).extra.get("lane_buffer")
        in_buffer = rec.cid in buffered.values() if isinstance(buffered, Mapping) else None
        if carrying and not in_buffer:
            return "agv"
        if in_buffer and not carrying:
            return "buffer"
        if carrying is None and in_buffer is False:
            return "agv"  # the STS set it down and not in the buffer
        if carrying is False and in_buffer is None:
            return "buffer"  # the STS set it down and not on the AGV
        return None

    def _rack_handover_done(self, rec: OrderRecord, eid: str) -> bool | None:
        """A crane job of ``rec`` failed (fault, unknown outcome): True if the equipment
        reports that the box had already been set down (the crane hoists up empty after the
        hand-over; re-issuing would pick a box that is no longer there), False if it is still
        at the source or in the spreader (re-issue), None if the equipment does not show it
        (``holding``, AGV load, ``lane_buffer``, rack occupancy missing): fail closed.
        AGV jobs hand over and complete in one step: always False."""
        cid = rec.cid
        crane = rec.stage is Stage.AT_LANE and eid == rec.order.sts_id
        stack = rec.stage in (Stage.STACKING, Stage.UNSTACKING) and eid == self._asc_by_block.get(rec.block)
        if not (crane or stack):
            return False
        extra = self.port.snapshot(eid).extra
        if "holding" not in extra:
            return None
        if extra["holding"] == cid:
            return False
        if crane:
            carrying = self._carrying(rec.agv_id, cid)
            if rec.order.type is JobType.LOAD:
                return None if carrying is None else not carrying  # not in the spreader nor on the AGV: on board
            buffered = extra.get("lane_buffer")
            if carrying:
                return True
            if not isinstance(buffered, Mapping):
                return None
            if cid in buffered.values():
                return True
            return False if carrying is False else None
        if rec.rack is None:
            return None
        on_rack = self._rack_occupied(rec.block).get(rec.rack) == cid
        return not on_rack if rec.stage is Stage.STACKING else on_rack

    def _rack_unknown(self, rec: OrderRecord, eid: str, reason: str) -> None:
        """Fail closed: the box of ``rec``'s running job cannot be located. Nothing is
        re-issued; the job's outcome is unknown (alarm) until ``resolve_job``, the device
        needs a release (also with ``auto_release``)."""
        job_id = rec.current_job_id
        assert job_id is not None
        log.error("%s: cannot tell where %s is after %s on %s (%s); waiting for resolve_job",
                  rec.cid, rec.cid, job_id, eid, reason)
        self._excluded.add(eid)
        self._unknown[job_id] = eid
        self._raise(AlarmKind.UNKNOWN_OUTCOME, job_id)
        self._need_release(eid, _UNKNOWN_CAUSE)

    def _rack_box_on_agv(self, rec: OrderRecord) -> bool:
        return rec.stage in BOX_ON_AGV or (rec.order.type is JobType.LOAD and rec.stage is Stage.AT_LANE)

    def _rack_completed(self, rec: OrderRecord, ev: EquipmentEvent) -> None:
        agv = rec.agv_id or ev.equipment_id
        match rec.stage:
            case Stage.TO_STS_LANE:
                rec.agv_at = f"LANE:{rec.lane}"
                self._enter(rec, Stage.AT_LANE)
            case Stage.AT_LANE if rec.order.type is JobType.DISCHARGE:
                if self._discharge_place(rec, ev.equipment_id) == "agv":
                    self._enter(rec, Stage.TO_RACK)
                    self._transfer(rec, "sts_to_agv", ev.equipment_id, rec.agv_id)
                else:  # the AGV was not ready: the box waits in the lane buffer
                    log.info("%s left in the buffer of lane %s; %s picks it up", rec.cid, rec.lane, rec.agv_id)
                    self._enter(rec, Stage.LANE_PICKUP)
            case Stage.AT_LANE:  # LOAD on board
                if self._transfer(rec, "agv_to_sts", agv, agv):
                    self._finish(rec)
            case Stage.LANE_PICKUP:
                self._enter(rec, Stage.TO_RACK)
            case Stage.BUFFER_WAIT:
                rec.agv_at = f"BUFFER:{rec.buffer}"
                self._enter(rec, Stage.BUFFER_WAIT)
            case Stage.TO_RACK:
                rec.avoid_racks.clear()
                rec.agv_id = rec.agv_at = None  # AGV free once the box is on the rack
                self._enter(rec, Stage.ON_RACK)
                self._transfer(rec, "agv_to_rack", agv, agv)
            case Stage.STACKING:
                self._finish(rec)
            case Stage.UNSTACKING:
                rec.avoid_racks.clear()
                self._enter(rec, Stage.ON_RACK)
                self._transfer(rec, "asc_to_rack", ev.equipment_id, None)
            case Stage.AGV_TO_RACK:
                rec.agv_at = f"RACK:{rec.rack}"
                self._drop_rack(rec)
                self._enter(rec, Stage.RACK_TO_STS)
                self._transfer(rec, "rack_to_agv", agv, agv)
            case Stage.RACK_TO_STS:
                rec.agv_at = f"LANE:{rec.lane}"
                self._enter(rec, Stage.AT_LANE)
            case _:
                log.error("completed %s for %s in unexpected stage %s", ev.job_id, rec.cid, rec.stage)

    def _rack_failed(self, rec: OrderRecord, ev: EquipmentEvent) -> bool:
        """Rack-mode failures that are not replans; True if handled."""
        if ev.reason in _RACK_RETRY and rec.stage in (Stage.TO_RACK, Stage.UNSTACKING):
            self._detach(rec)
            if rec.rack is not None:
                rec.avoid_racks.add(rec.rack)
            log.info("%s: rack %s occupied for %s; picking another", ev.equipment_id, rec.rack, rec.cid)
            self._drop_rack(rec)
            if rec.stage is Stage.UNSTACKING:
                self._enter(rec, Stage.AWAIT_ASC)
            return True
        if ev.reason in _RESLOT_REASONS and rec.stage is Stage.STACKING:
            self._detach(rec)
            self._enter(rec, Stage.ON_RACK)
            self._reslot(rec)
            return True
        if ev.reason == "SLOT_EMPTY" and rec.stage is Stage.UNSTACKING:
            self._detach(rec)
            log.error("slot %s empty for load %s; failing order", rec.yard_slot, rec.cid)
            self._fail_order(rec, "SLOT_EMPTY")
            return True
        return False

    def _rack_rollback(self, rec: OrderRecord, old: Stage) -> None:
        if old in RACK_RELEASE_AGV:
            rec.agv_id = rec.agv_at = None
            self._free_lane(rec)
        if old is Stage.TO_RACK:
            self._drop_rack(rec)  # picked again (the AGV's booking ended with its job)
        if old is Stage.BUFFER_WAIT and rec.buffer is not None:
            if self._buffer_agv.get(rec.buffer) == rec.agv_id:
                del self._buffer_agv[rec.buffer]
            rec.buffer = rec.agv_at = None
        rec.stage = RACK_ROLLBACK.get(old, old)
