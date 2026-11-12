"""Automated stacking crane of the v2 world: one of a pair (``role`` WS or LS) on one block track.

Jobs (``terminal_sim.loc2`` grammar, columns 0-based):
- WS: ``STACK_IN`` ``RACK:<id>`` -> ``SLOT:<block>:<bay>:<row>`` (on top of the column),
  ``STACK_OUT`` slot -> rack, ``STACK_MOVE`` slot -> slot.
- LS: ``STACK_MOVE``, ``TRUCK_IN`` ``TRUCK:<block>:<lane>`` -> slot, ``TRUCK_OUT`` slot -> truck;
  a truck job lasts ``truck_cycle_s`` (no truck model: the time left after the crane's own
  motions is held at the end, standing for the truck handling).

A cycle: gantry + trolley (simultaneous) to the pick point, hoist down empty, lock
(``spreader_lock_s``), hoist up laden, to the place point, hoist down laden, unlock, hoist up.
Gantry moves are claimed on the ``BlockTrack``; a refused claim asks the partner to yield
and waits; a waiting LS crane honours the WS crane's requests (WS priority), an idle crane
honours any request (BUSY, without job events, like the legacy AGV auto-clear).
"""
from __future__ import annotations

import random
from collections.abc import Callable, Iterator

import salabim
from terminal_model.layout import Block, TransferZone, TruckHead
from terminal_model.port import EquipmentEvent
from terminal_model.types import Job, JobType, Kind, Position, State

from terminal_sim.equipment.base import Equipment
from terminal_sim.equipment.block_track import LS, WS, BlockTrack
from terminal_sim.equipment.motion import sampled_hold
from terminal_sim.kinematics import AscKin, Axis
from terminal_sim.loc2 import RackLoc, SlotLoc2, TruckLoc, try_parse
from terminal_sim.stack import Stack

TRACK_POLL_S = 1.0
TRACK_MARGIN_S = 30.0  # a gantry claim expires this long after its planned end
RACK_H_M = 1.5
TRUCK_H_M = 1.5
TRUCK_CYCLE_S = 180.0
ENERGY_PER_MOVE_KWH = 2.5
_WS_JOBS = {JobType.STACK_IN, JobType.STACK_OUT, JobType.STACK_MOVE}
_LS_JOBS = {JobType.STACK_MOVE, JobType.TRUCK_IN, JobType.TRUCK_OUT}


class Asc2(Equipment):
    def __init__(
        self, *, equipment_id: str, role: str, partner: str, rng: random.Random,
        emit: Callable[[EquipmentEvent], None], env: salabim.Environment, block: Block,
        zone: TransferZone, truck: TruckHead | None, kin: AscKin, stack: Stack, track: BlockTrack,
        hub, start_bay: float, truck_cycle_s: float = TRUCK_CYCLE_S,
    ) -> None:
        if role not in (WS, LS):
            raise ValueError(f"role must be WS or LS, got {role!r}")
        self.block = block
        self.gantry_bay = float(start_bay)
        self.trolley_m = block.width_m / 2
        super().__init__(equipment_id=equipment_id, kind=Kind.ASC, position=self._xy(),
                         rng=rng, emit=emit, env=env)
        self.role, self.partner = role, partner
        self.zone = zone
        self.rack_ids = {r.id: r for r in zone.racks}
        self.truck = truck
        self.kin = kin
        self.stack = stack
        self.track = track
        self.hub = hub
        self.truck_cycle_s = truck_cycle_s
        self.travel_h = (block.tiers + 1) * block.tier_h_m
        self.hoist_m = self.travel_h
        self.tier: int | None = None
        self.holding: str | None = None
        self._yielding = False
        track.register(equipment_id, role, self.gantry_bay, on_yield=self._on_yield,
                       on_expire=self._on_expire)

    # --- geometry -------------------------------------------------------------
    def _xy(self) -> Position:
        b = self.block
        return Position(b.x0_m + self.trolley_m, b.y0_m + (self.gantry_bay + 0.5) * b.bay_pitch_m)

    def bay_of_y(self, y: float) -> float:
        return (y - self.block.y0_m) / self.block.bay_pitch_m - 0.5

    def rack_point(self, rack_id: str) -> tuple[float, float, float]:
        r = self.rack_ids[rack_id]
        return self.bay_of_y(r.y_m), r.x_m - self.block.x0_m, RACK_H_M

    def slot_point(self, bay: int, row: int, tier: int) -> tuple[float, float, float]:
        return float(bay), (row + 0.5) * self.block.row_pitch_m, tier * self.block.tier_h_m

    def truck_point(self, lane: int) -> tuple[float, float, float]:
        assert self.truck is not None
        y = self.truck.y_m + (self.truck.len_m / 2 if self.truck.len_m > 0 else 10.0)
        lanes = max(self.truck.lanes, 1)
        return self.bay_of_y(y), (lane + 0.5) * self.block.width_m / lanes, TRUCK_H_M

    @property
    def row(self) -> int:
        r = int(self.trolley_m / self.block.row_pitch_m)
        return min(max(r, 0), self.block.rows - 1)

    # --- acceptance -----------------------------------------------------------
    def _own_slot(self, loc) -> bool:
        b = self.block
        return (isinstance(loc, SlotLoc2) and loc.block == b.id and loc.bay < b.bays
                and loc.row < b.rows and (loc.tier is None or loc.tier < b.tiers))

    def _own_rack(self, loc) -> bool:
        return isinstance(loc, RackLoc) and loc.rack_id in self.rack_ids

    def _own_truck(self, loc) -> bool:
        return (isinstance(loc, TruckLoc) and self.truck is not None and loc.block == self.block.id
                and loc.lane < max(self.truck.lanes, 1))

    def accepts(self, job: Job) -> bool:
        if job.type not in (_WS_JOBS if self.role == WS else _LS_JOBS):
            return False
        src, dst = try_parse(job.from_loc), try_parse(job.to_loc)
        return {
            JobType.STACK_IN: lambda: self._own_rack(src) and self._own_slot(dst),
            JobType.STACK_OUT: lambda: self._own_slot(src) and self._own_rack(dst),
            JobType.STACK_MOVE: lambda: self._own_slot(src) and self._own_slot(dst),
            JobType.TRUCK_IN: lambda: self._own_truck(src) and self._own_slot(dst),
            JobType.TRUCK_OUT: lambda: self._own_slot(src) and self._own_truck(dst),
        }[job.type]() and (self.holding is None or job.container_id == self.holding)

    # --- aborts / faults ------------------------------------------------------
    def _stop_motion(self) -> None:
        self.track.release(self.equipment_id, at=self.gantry_bay)
        self.hub.release_racks(self.equipment_id)

    def _on_expire(self) -> None:
        """The track claim outlived its deadline: stop where the crane is and fail the job
        (``TRACK_TIMEOUT``); an idle yield move just stops."""
        if self.current_job is not None:
            self.fail_job("TRACK_TIMEOUT")
        elif self._yielding:
            if self.env.current_component() is not self:
                self.cancel()
            self._yielding = False
            self._stop_motion()
            self.set_state(State.IDLE)

    def fail_job(self, reason: str) -> None:
        super().fail_job(reason)
        self._stop_motion()

    def cancel_job(self) -> bool:
        ok = super().cancel_job()
        self._stop_motion()
        return ok

    def set_state(self, s: State) -> None:
        if s is State.FAULT and self._yielding:
            if self.env.current_component() is not self:
                self.cancel()
            self._yielding = False
            self._stop_motion()
        super().set_state(s)
        if s is State.IDLE:
            self._on_yield()

    # --- yielding -------------------------------------------------------------
    def _on_yield(self) -> None:
        if (self.track.pending_yield(self.equipment_id) is not None and self.state is State.IDLE
                and self.current_job is None and not self._yielding
                and self.env.current_component() is not self):
            self.activate(process="_run")

    def _idle_behaviour(self):
        if self.track.pending_yield(self.equipment_id) is None:
            return None
        return self._idle_yield()

    def _idle_yield(self) -> Iterator:
        self._yielding = True
        self.set_state(State.BUSY)
        yield from self._yield_move()
        self._yielding = False
        self.set_state(State.IDLE)

    def _yield_move(self) -> Iterator:
        target = self.track.pending_yield(self.equipment_id)
        self.track.clear_yield(self.equipment_id)
        if target is None:
            return
        # Yielding only ever moves away from the partner. A target on the partner's side is
        # stale (asked while this crane was moving; the track knew only its start bay), and
        # driving there could wait forever for a partner parked in the way.
        if (target > self.gantry_bay) if self.role == WS else (target < self.gantry_bay):
            return
        # Moving away from a waiting partner: the claim succeeds unless the partner moves.
        while not self.track.claim(self.equipment_id, self.gantry_bay, target,
                                   self.env.now() + self._move_time(target, self.trolley_m)
                                   + TRACK_MARGIN_S):
            self.track.expire(self.env.now())
            yield self.hold(TRACK_POLL_S)
        yield from self._move(target, self.trolley_m)

    # --- motion ----------------------------------------------------------------
    def _move(self, bay: float, trolley: float) -> Iterator:
        """Gantry + trolley to (bay, trolley); the track claim must be held. Releases it."""
        pitch = self.block.bay_pitch_m
        g0, t0 = self.gantry_bay, self.trolley_m
        dg, dt = (bay - g0) * pitch, trolley - t0
        T = max(self.kin.gantry.travel_time(abs(dg)), self.kin.trolley.travel_time(abs(dt)))

        def upd(t: float) -> None:
            sg = self.kin.gantry.position_at(abs(dg), t)
            st = self.kin.trolley.position_at(abs(dt), t)
            self.gantry_bay = g0 + (sg if dg >= 0 else -sg) / pitch
            self.trolley_m = t0 + (st if dt >= 0 else -st)
            self.position = self._xy()

        yield from sampled_hold(self, T, upd)
        self.gantry_bay, self.trolley_m = bay, trolley
        self.position = self._xy()
        self.track.release(self.equipment_id, at=bay)

    def _move_time(self, bay: float, trolley: float) -> float:
        return max(self.kin.gantry.travel_time(abs(bay - self.gantry_bay) * self.block.bay_pitch_m),
                   self.kin.trolley.travel_time(abs(trolley - self.trolley_m)))

    def _goto(self, bay: float, trolley: float) -> Iterator:
        while not self.track.claim(self.equipment_id, self.gantry_bay, bay,
                                   self.env.now() + self._move_time(bay, trolley) + TRACK_MARGIN_S):
            self.track.expire(self.env.now())
            self.track.yield_request(self.equipment_id, self.partner)
            if self.role == LS and self.track.pending_yield(self.equipment_id) is not None:
                yield from self._yield_move()
                continue
            yield self.hold(TRACK_POLL_S)
        yield from self._move(bay, trolley)

    def _hoist(self, to_h: float, axis: Axis) -> Iterator:
        h0 = self.hoist_m
        d = to_h - h0

        def upd(t: float) -> None:
            s = axis.position_at(abs(d), t)
            self.hoist_m = h0 + (s if d >= 0 else -s)

        yield from sampled_hold(self, axis.travel_time(abs(d)), upd)
        self.hoist_m = to_h

    # --- process ------------------------------------------------------------------
    def _check(self, job: Job, src, dst) -> str | None:
        """Failure reason, or None if the job can start. A box held in the spreader (left there
        by a failed job) is only accepted as the job's own box; then the pick is skipped."""
        cid = job.container_id
        if self.holding is not None and self.holding != cid:
            return "HOLDING_BOX"
        held = self.holding == cid
        if held:
            pass
        elif isinstance(src, RackLoc) and self.hub.rack_occupant(src.rack_id) != cid:
            # elsewhere (e.g. already stacked by an earlier run of this job) / not there at all
            return "BOX_NOT_AT_SOURCE" if self.hub.locate_box(cid) is not None else "RACK_EMPTY"
        elif isinstance(src, SlotLoc2):
            loc = self.stack.locate(cid)
            if loc is None or loc[:3] != (src.block, src.bay, src.row):
                return "SLOT_EMPTY"
            if src.tier is not None and loc[3] != src.tier:
                return "SLOT_EMPTY"
            if self.stack.top(src.block, src.bay, src.row) != cid:
                return "NOT_ON_TOP"
        elif isinstance(src, TruckLoc) and self.stack.locate(cid) is not None:
            return "CONTAINER_PLACED"
        if isinstance(dst, SlotLoc2):
            h = self.stack.column_height(dst.block, dst.bay, dst.row)
            if (not held and isinstance(src, SlotLoc2)
                    and (src.bay, src.row) == (dst.bay, dst.row)):
                return "SAME_COLUMN"
            if h >= self.block.tiers:
                return "COLUMN_FULL"
            if dst.tier is not None and dst.tier != h:
                return "TIER_NOT_ON_TOP"
        if isinstance(dst, RackLoc) and not self.hub.reserve_rack(dst.rack_id, self.equipment_id):
            return "RACK_OCCUPIED"  # booked before the pick: the set-down cannot collide
        return None

    def _point(self, loc) -> tuple[float, float, float]:
        if isinstance(loc, RackLoc):
            return self.rack_point(loc.rack_id)
        if isinstance(loc, TruckLoc):
            return self.truck_point(loc.lane)
        if isinstance(loc, SlotLoc2):
            h = self.stack.column_height(loc.block, loc.bay, loc.row)
            # pick: the top box (h - 1); place: on top (h)
            tier = h - 1 if self.stack.top(loc.block, loc.bay, loc.row) == self._cid else h
            return self.slot_point(loc.bay, loc.row, max(tier, 0))
        raise ValueError(loc)

    def process(self):
        job = self.current_job
        assert job is not None
        src, dst = try_parse(job.from_loc), try_parse(job.to_loc)
        reason = self._check(job, src, dst)
        if reason is not None:
            self.fail_job(reason)
            return
        cid = self._cid = job.container_id
        start = self.env.now()
        kin = self.kin
        if self.holding != cid:  # pick (skipped for a box already in the spreader)
            bay, trolley, h = self._point(src)
            yield from self._goto(bay, trolley)
            yield from self._hoist(h, kin.hoist_empty)
            yield self.hold(kin.spreader_lock_s)
            if isinstance(src, RackLoc):
                if self.hub.rack_occupant(src.rack_id) != cid:  # an AGV took it meanwhile
                    self.fail_job("RACK_EMPTY")
                    return
                self.hub.transfer_rack_to_asc(self, src.rack_id, cid)
            elif isinstance(src, SlotLoc2):
                if self.stack.top(src.block, src.bay, src.row) != cid:
                    self.fail_job("NOT_ON_TOP")
                    return
                self.tier = self.stack.take(cid)[3]
                self.holding = cid
            else:
                self.holding = cid  # from the truck
        yield from self._hoist(self.travel_h, kin.hoist_laden)
        bay, trolley, h = self._point(dst)
        yield from self._goto(bay, trolley)
        if isinstance(dst, SlotLoc2):
            _, _, h = self._point(dst)  # the column may have changed meanwhile
        yield from self._hoist(h, kin.hoist_laden)
        yield self.hold(kin.spreader_lock_s)
        if isinstance(dst, RackLoc):
            self.hub.transfer_asc_to_rack(self, dst.rack_id, cid)
        elif isinstance(dst, SlotLoc2):
            try:
                self.tier = self.stack.put(dst.block, dst.bay, dst.row, cid)[3]
            except ValueError:
                self.fail_job("COLUMN_FULL")  # box stays in the spreader
                return
            self.holding = None
        else:
            self.holding = None  # onto the truck
        yield from self._hoist(self.travel_h, kin.hoist_empty)
        if job.type in (JobType.TRUCK_IN, JobType.TRUCK_OUT):
            rest = self.truck_cycle_s - (self.env.now() - start)
            if rest > 0:
                yield self.hold(rest)
        self.energy_kwh += ENERGY_PER_MOVE_KWH
        self.complete_job()

    _cid: str | None = None

    def extra(self) -> dict:
        return {
            "block_id": self.block.id,
            "gantry_position_m": (self.gantry_bay + 0.5) * self.block.bay_pitch_m,
            "bay": round(self.gantry_bay, 3),
            "row": self.row,
            "tier": self.tier,
            "trolley_pos_m": self.trolley_m,
            "hoist_pos_m": self.hoist_m,
            "partner": self.partner,
            "role": self.role,
            "holding": self.holding,
        }
