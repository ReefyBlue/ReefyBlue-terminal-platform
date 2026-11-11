"""Ship-to-shore crane of the v2 world: gantry, trolley and hoist on kinematic axes, transfer
lanes with a one-box lane buffer each.

Jobs: ``DISCHARGE`` ``VESSEL:<berth>:<bay>:<row>:<tier>`` -> ``LANE:<id>`` and ``LOAD`` reversed
(``LANE:*`` = any lane of this crane). A move: gantry to the bay (waiting while a neighbour is
closer than ``min_gap_m``), then the vessel move (``Vessel.move_segments``), ``twistlock_s`` over
the lane, and the handover:

- DISCHARGE: the box goes on the idle Lift-AGV standing in the lane that was sent for it, else
  into the empty lane buffer (the late AGV picks it up itself). With neither the crane waits
  (``wait_for_agv_s``). Before the move it also waits while the lane cannot take the box.
- LOAD: the crane waits until the box is in the lane (on an AGV there or in the lane buffer).

Passive towards the AGVs: the crane never moves them; the world (``hub``) does the transfer.
"""
from __future__ import annotations

import random
from collections.abc import Callable, Iterator

import salabim
from terminal_model.layout import Lane
from terminal_model.port import EquipmentEvent
from terminal_model.types import Job, JobType, Kind, Position

from terminal_sim.equipment.base import Equipment
from terminal_sim.equipment.motion import sampled_hold
from terminal_sim.equipment.vessel import Segment, Vessel
from terminal_sim.kinematics import StsKin
from terminal_sim.loc2 import LaneLoc, VesselLoc, try_parse

STS_POLL_S = 1.0
ENERGY_PER_MOVE_KWH = 5.0


def lane_point_y(lane: Lane) -> float:
    """y of a lane's handover point (lane centre)."""
    return lane.y_m + lane.len_m / 2


class Sts2(Equipment):
    def __init__(
        self, *, equipment_id: str, rng: random.Random, emit: Callable[[EquipmentEvent], None],
        env: salabim.Environment, x0_m: float, rail_y_m: float, min_gap_m: float,
        lanes: tuple[Lane, ...], berth_ids: tuple[str, ...], vessels: dict[str, Vessel],
        kin: StsKin, hub,
    ) -> None:
        super().__init__(equipment_id=equipment_id, kind=Kind.STS, position=Position(x0_m, rail_y_m),
                         rng=rng, emit=emit, env=env)
        self.x0_m = x0_m
        self.x_m = x0_m
        self.rail_y_m = rail_y_m
        self.min_gap_m = min_gap_m
        self.lanes: dict[str, Lane] = {ln.id: ln for ln in lanes}
        self.lane_buffer: dict[str, str | None] = {ln.id: None for ln in lanes}
        self.berth_ids = berth_ids
        self.vessels = vessels
        self.kin = kin
        self.hub = hub
        first = lanes[0]
        self.trolley_pos_m = lane_point_y(first)
        self.hoist_pos_m = 10.0
        self.lane_id: str | None = None
        self.gantry_claim: tuple[float, float] | None = None
        self.wait_for_agv_s = 0.0  # of the current / last move
        self.wait_for_agv_total_s = 0.0
        self.moves = 0

    # --- acceptance ---------------------------------------------------------
    def _parse(self, job: Job) -> tuple[VesselLoc, LaneLoc] | None:
        if job.type is JobType.DISCHARGE:
            v, ln = try_parse(job.from_loc), try_parse(job.to_loc)
        elif job.type is JobType.LOAD:
            ln, v = try_parse(job.from_loc), try_parse(job.to_loc)
        else:
            return None
        if not isinstance(v, VesselLoc) or not isinstance(ln, LaneLoc):
            return None
        return v, ln

    def accepts(self, job: Job) -> bool:
        parsed = self._parse(job)
        if parsed is None:
            return False
        v, ln = parsed
        if v.berth not in self.vessels or (self.berth_ids and v.berth not in self.berth_ids):
            return False
        try:
            self.vessels[v.berth].check(v.bay, v.row, v.tier)
        except ValueError:
            return False
        return ln.lane_id == "*" or ln.lane_id in self.lanes

    # --- abort ----------------------------------------------------------------
    def fail_job(self, reason: str) -> None:
        self.gantry_claim = None
        super().fail_job(reason)

    def cancel_job(self) -> bool:
        self.gantry_claim = None
        return super().cancel_job()

    # --- motion ---------------------------------------------------------------
    def extent(self) -> tuple[float, float]:
        return self.gantry_claim if self.gantry_claim is not None else (self.x_m, self.x_m)

    def _gantry(self, target: float) -> Iterator:
        lo, hi = min(self.x_m, target), max(self.x_m, target)
        while not self.hub.gantry_free(self, lo, hi):
            yield self.hold(STS_POLL_S)
        self.gantry_claim = (lo, hi)
        x0, d = self.x_m, target - self.x_m

        def upd(t: float) -> None:
            s = self.kin.gantry.position_at(abs(d), t)
            self.x_m = x0 + (s if d >= 0 else -s)
            self.position = Position(self.x_m, self.rail_y_m)

        yield from sampled_hold(self, self.kin.gantry.travel_time(abs(d)), upd)
        self.x_m = target
        self.position = Position(self.x_m, self.rail_y_m)
        self.gantry_claim = None

    def _segment(self, seg: Segment) -> Iterator:
        def upd(t: float) -> None:
            self.trolley_pos_m, self.hoist_pos_m = seg.at(t)

        yield from sampled_hold(self, seg.duration, upd)
        self.trolley_pos_m, self.hoist_pos_m = seg.trolley_to, seg.hoist_to

    def _wait_agv(self, cond: Callable[[], bool]) -> Iterator:
        while not cond():
            yield self.hold(STS_POLL_S)
            self.wait_for_agv_s += STS_POLL_S
            self.wait_for_agv_total_s += STS_POLL_S

    def _pick_lane(self, want: str, ok: Callable[[str], bool]) -> str | None:
        if want != "*":
            return want if ok(want) else None
        return next((ln for ln in self.lanes if ok(ln)), None)

    # --- process ----------------------------------------------------------------
    def process(self):
        job = self.current_job
        assert job is not None
        parsed = self._parse(job)
        if parsed is None:
            self.fail_job("BAD_LOCATION")
            return
        v, want = parsed
        vessel = self.vessels[v.berth]
        cid = job.container_id
        self.wait_for_agv_s = 0.0
        target_x = vessel.bay_x(v.bay)
        if abs(target_x - self.x_m) > 1e-6:
            yield from self._gantry(target_x)
        discharge = job.type is JobType.DISCHARGE
        if discharge:
            def ok(ln: str) -> bool:
                return self.hub.lane_accepts(self, ln, cid)
        else:
            def ok(ln: str) -> bool:
                return self.hub.lane_has_box(self, ln, cid)
        chosen: list[str] = []

        def pick() -> bool:
            ln = self._pick_lane(want.lane_id, ok)
            if ln is not None:
                chosen[:] = [ln]
            return ln is not None

        yield from self._wait_agv(pick)
        lane = chosen[0]
        self.lane_id = lane
        lane_y = lane_point_y(self.lanes[lane])
        segs = vessel.move_segments(self.kin, v.row, v.tier, not discharge, lane_y_m=lane_y)
        if discharge:
            for seg in segs[:-1]:
                yield from self._segment(seg)
            yield self.hold(self.kin.twistlock_s)
            yield from self._wait_agv(lambda: ok(lane))
            self.hub.transfer_sts_to_lane(self, lane, cid)
            yield from self._segment(segs[-1])
        else:
            yield from self._segment(segs[0])
            yield self.hold(self.kin.twistlock_s)
            yield from self._wait_agv(lambda: ok(lane))
            self.hub.transfer_lane_to_sts(self, lane, cid)
            for seg in segs[1:]:
                yield from self._segment(seg)
            vessel.loaded.append(cid)
        self.energy_kwh += ENERGY_PER_MOVE_KWH
        self.moves += 1
        self.complete_job()

    def extra(self) -> dict:
        return {
            "berth_side": "WS",
            "spreader_height_m": self.hoist_pos_m,
            "trolley_position_m": self.trolley_pos_m,
            "handover_lane_occupied": any(v is not None for v in self.lane_buffer.values()),
            "gantry_x_m": self.x_m,
            "trolley_pos_m": self.trolley_pos_m,
            "hoist_pos_m": self.hoist_pos_m,
            "lane_id": self.lane_id,
            "lane_buffer": dict(self.lane_buffer),
            "lane_buffer_count": sum(v is not None for v in self.lane_buffer.values()),
            "wait_for_agv_s": self.wait_for_agv_s,
            "wait_for_agv_total_s": self.wait_for_agv_total_s,
        }
