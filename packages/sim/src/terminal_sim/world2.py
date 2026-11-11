"""The v2 simulation world: STS with transfer lanes and lane buffers, Lift-AGVs with transfer
racks, an ASC pair per block on one track, a column ``Stack`` (spec C1 §5).

Same public surface as the legacy ``World`` (``port``, ``equipment``, ``run``, ``now``, ``yard``,
``layout``, ``scenario``, ``faults_total``, ``fault_injectors``) so the sim port, the OPC UA
servers and the fault injector work unchanged; built by ``make_world`` when the layout carries
its metre layout (``Layout.terminal``).

Equipment order (= OPC UA port order): STS (``n_sts``), ``AGV-01..`` (``n_agv``, starting on the
buffer cells of the grid view), then per block its WS then LS ASC.

The world is the handover hub: equipment calls ``transfer_*`` when a box changes hands, so the
lane buffers, racks and stack are always consistent. AGVs drive the legacy ``Grid`` on the
derived grid; cells holding lane or rack points are shared (any number of AGVs), the points
themselves are exclusive (``reserve_point``).
"""
from __future__ import annotations

import logging
import random

import salabim
from terminal_model.layout import TerminalLayout
from terminal_model.layout_grid import cell_of
from terminal_model.locations import SlotLoc
from terminal_model.types import Position

from terminal_sim.equipment.asc2 import Asc2
from terminal_sim.equipment.base import Equipment
from terminal_sim.equipment.block_track import LS, WS, BlockTrack
from terminal_sim.equipment.lift_agv import LiftAGV
from terminal_sim.equipment.racks import Racks
from terminal_sim.equipment.sts2 import Sts2, lane_point_y
from terminal_sim.equipment.vessel import Vessel
from terminal_sim.faults import FaultInjector
from terminal_sim.grid import Cell, Grid
from terminal_sim.kinematics import kin_for
from terminal_sim.layout import Layout
from terminal_sim.loc2 import BufferLoc, LaneLoc, RackLoc, parse_loc2
from terminal_sim.scenario import Scenario
from terminal_sim.stack import Stack
from terminal_sim.world import SimEquipmentPort

log = logging.getLogger("terminal_sim.world2")


class ZoneGrid(Grid):
    """``Grid`` whose ``shared`` cells take any number of AGVs (never reserved)."""

    def __init__(self, cols: int, rows: int, cell_m: float, shared: frozenset[Cell]) -> None:
        super().__init__(cols, rows, cell_m)
        self.shared = shared

    def try_reserve(self, c: Cell, owner: str) -> bool:
        if c in self.shared:
            self._check(c)
            return True
        return super().try_reserve(c, owner)

    def release(self, c: Cell, owner: str) -> None:
        if c not in self.shared:
            super().release(c, owner)


class StackYard:
    """Legacy ``Yard`` interface (1-based ``SlotLoc``) over the column ``Stack`` for
    ``PrefillYard`` and the runner's prefill: a slot can only be occupied on top of its column."""

    def __init__(self, stack: Stack, t: TerminalLayout) -> None:
        self.stack = stack
        self._blocks = {b.id: b for b in t.blocks}

    def _col(self, slot: SlotLoc) -> tuple[str, int, int]:
        b = self._blocks[slot.block]  # KeyError for an unknown block
        if not (1 <= slot.bay <= b.bays and 1 <= slot.row <= b.rows and 1 <= slot.tier <= b.tiers):
            raise ValueError("SLOT_OUT_OF_RANGE")
        return slot.block, slot.bay - 1, slot.row - 1

    def is_free(self, slot: SlotLoc) -> bool:
        return self.stack.column_height(*self._col(slot)) < slot.tier

    def occupy(self, slot: SlotLoc, container_id: str) -> None:
        col = self._col(slot)
        if self.stack.column_height(*col) != slot.tier - 1:
            raise ValueError("SLOT_OCCUPIED" if not self.is_free(slot) else "SLOT_NOT_ON_TOP")
        if self.stack.locate(container_id) is not None:
            raise ValueError("CONTAINER_PLACED")
        self.stack.put(*col, container_id)

    def release(self, slot: SlotLoc) -> str:
        col = self._col(slot)
        cid = self.stack.top(*col)
        if cid is None or self.stack.column_height(*col) != slot.tier:
            raise KeyError(slot)
        self.stack.take(cid)
        return cid

    def locate(self, container_id: str) -> SlotLoc | None:
        loc = self.stack.locate(container_id)
        if loc is None:
            return None
        return SlotLoc(loc[0], loc[1] + 1, loc[2] + 1, loc[3] + 1)

    def occupancy(self, block: str) -> float:
        return self.stack.occupancy(block)


class World2:
    def __init__(self, layout: Layout, scenario: Scenario) -> None:
        t = layout.terminal
        if t is None:
            raise ValueError("World2 needs a v2 layout (Layout.terminal)")
        self.layout = layout
        self.terminal = t
        self.scenario = scenario
        self.env = salabim.Environment(trace=False, yieldless=False, random_seed=scenario.seed)
        self.rng = random.Random(scenario.seed)
        cell_m = layout.grid_cell_m
        self.lanes = {ln.id: (s.id, ln) for s in t.sts for ln in s.lanes}
        self.rack_specs = {r.id: (z.block, r) for z in t.transfer_zones for r in z.racks}
        self.lane_points = {lid: Position(ln.x_m, lane_point_y(ln)) for lid, (_, ln) in self.lanes.items()}
        self.rack_points = {rid: Position(r.x_m, r.y_m) for rid, (_, r) in self.rack_specs.items()}
        self.zone_cells = frozenset(
            cell_of(p.x, p.y, cell_m) for p in [*self.lane_points.values(), *self.rack_points.values()])
        self.grid = ZoneGrid(layout.grid_cols, layout.grid_rows, cell_m, self.zone_cells)
        order = {s.id: i for i, s in enumerate(t.sts)}
        buffers = sorted(t.apron.buffers, key=lambda b: order.get(b.sts_id, len(order)))
        self.buffer_cells: dict[str, Cell] = {
            b.id: c for b, c in zip(buffers, layout.agv_start_cells, strict=False)}
        self.stack = Stack(t.blocks)
        self.racks = Racks(t.transfer_zones)
        self.vessels = {b.id: Vessel(b, bays=scenario.vessel_bays) for b in t.quay.berths}
        self.yard = StackYard(self.stack, t)
        self.points: dict[str, str] = {}  # lane/rack id -> AGV id
        self.rack_res: dict[str, str] = {}  # rack id -> equipment id that will set a box on it
        self.port = SimEquipmentPort(self)
        self.equipment: dict[str, Equipment] = {}
        emit = self.port._dispatch

        self.sts: list[Sts2] = []
        for s in t.sts[: scenario.n_sts]:
            sts = Sts2(
                equipment_id=s.id, rng=self.rng, emit=emit, env=self.env, x0_m=s.x0_m,
                rail_y_m=t.sts_rails.y_ws_m, min_gap_m=s.min_gap_m, lanes=s.lanes,
                berth_ids=s.berth_ids, vessels=self.vessels, kin=kin_for(t, s.id, "STS"), hub=self)
            self.equipment[s.id] = sts
            self.sts.append(sts)
        self.sts.sort(key=lambda s: s.x0_m)
        self.agvs: dict[str, LiftAGV] = {}
        for i, cell in enumerate(layout.agv_start_cells[: scenario.n_agv], start=1):
            agv_id = f"AGV-{i:02d}"
            agv = LiftAGV(equipment_id=agv_id, rng=self.rng, emit=emit, env=self.env, start_cell=cell,
                          grid=self.grid, kin=kin_for(t, agv_id, "AGV"), hub=self,
                          twin_20=scenario.twin_20)
            self.equipment[agv_id] = self.agvs[agv_id] = agv
        self.tracks: dict[str, BlockTrack] = {}
        for b in t.blocks:
            zone = t.transfer_zone(b.id)
            truck = next((h for h in t.landside if h.block == b.id), None)
            cranes = {}
            for role, eid, partner, start in ((WS, b.asc_ws_id, b.asc_ls_id, 0.0),
                                              (LS, b.asc_ls_id, b.asc_ws_id, b.bays - 1.0)):
                cranes[role] = (eid, partner, start)
            probe = _geometry(b, zone, truck)
            track = BlockTrack(b.bays, b.asc_min_gap_bays, lo=probe[0], hi=probe[1])
            self.tracks[b.id] = track
            for role, (eid, partner, start) in cranes.items():
                self.equipment[eid] = Asc2(
                    equipment_id=eid, role=role, partner=partner, rng=self.rng, emit=emit,
                    env=self.env, block=b, zone=zone, truck=truck, kin=kin_for(t, eid, "ASC"),
                    stack=self.stack, track=track, hub=self, start_bay=start)

        self.fault_injectors: list[FaultInjector] = []
        for eq in self.equipment.values():
            params = scenario.faults.get(eq.kind)
            if params is not None and params.mtbf_s > 0:
                self.fault_injectors.append(
                    FaultInjector(target=eq, params=params, rng=self.rng, env=self.env))

    # --- World surface --------------------------------------------------------------
    def faults_total(self) -> int:
        return sum(i.count for i in self.fault_injectors)

    def run(self, until: float) -> None:
        self.env.run(till=until)

    def now(self) -> float:
        return self.env.now()

    # --- STS gantry -----------------------------------------------------------------
    def gantry_free(self, sts: Sts2, lo: float, hi: float) -> bool:
        """True if ``sts`` may occupy [lo, hi] keeping ``min_gap_m`` to its neighbours."""
        i = self.sts.index(sts)
        for j in (i - 1, i + 1):
            if not 0 <= j < len(self.sts):
                continue
            other = self.sts[j]
            gap = max(sts.min_gap_m, other.min_gap_m)
            olo, ohi = other.extent()
            if j < i and lo - gap < ohi - 1e-9:
                return False
            if j > i and hi + gap > olo + 1e-9:
                return False
        return True

    # --- AGV targets and points -------------------------------------------------------
    def resolve_agv(self, loc: str) -> tuple[Cell, str | None, Position | None, str]:
        """(goal cell, point id, point xy, kind) of an AGV target; ValueError if unknown."""
        p = parse_loc2(loc)
        cm = self.layout.grid_cell_m
        if isinstance(p, LaneLoc) and p.lane_id in self.lanes:
            xy = self.lane_points[p.lane_id]
            return cell_of(xy.x, xy.y, cm), p.lane_id, xy, "LANE"
        if isinstance(p, RackLoc) and p.rack_id in self.rack_specs:
            xy = self.rack_points[p.rack_id]
            return cell_of(xy.x, xy.y, cm), p.rack_id, xy, "RACK"
        if isinstance(p, BufferLoc) and p.buffer_id in self.buffer_cells:
            return self.buffer_cells[p.buffer_id], None, None, "BUFFER"
        raise ValueError(f"unknown AGV target {loc!r}")

    def reserve_point(self, point: str, agv_id: str) -> bool:
        holder = self.points.get(point)
        if holder is not None and holder != agv_id:
            return False
        self.points[point] = agv_id
        return True

    def release_point(self, point: str, agv_id: str) -> None:
        if self.points.get(point) == agv_id:
            del self.points[point]

    def agv_at(self, point: str) -> LiftAGV | None:
        return next((a for a in self.agvs.values() if a.at_point == point), None)

    # --- lanes ------------------------------------------------------------------------
    def _sts_of_lane(self, lane_id: str) -> Sts2:
        sts = self.equipment[self.lanes[lane_id][0]]
        assert isinstance(sts, Sts2)
        return sts

    def lane_buffer_of(self, lane_id: str) -> str | None:
        return self._sts_of_lane(lane_id).lane_buffer[lane_id]

    def _waiting_agv(self, lane_id: str, cid: str) -> LiftAGV | None:
        """Idle AGV standing at the lane that was sent for ``cid`` and can take it."""
        agv = self.agv_at(lane_id)
        if (agv is not None and agv.current_job is None and agv.state.value == "IDLE"
                and agv.job_container_id == cid and agv.free_slot() is not None):
            return agv
        return None

    def lane_accepts(self, sts: Sts2, lane_id: str, cid: str) -> bool:
        """A buffered box blocks its lane: nothing is set down there until it is fetched."""
        return sts.lane_buffer[lane_id] is None

    def lane_has_box(self, sts: Sts2, lane_id: str, cid: str) -> bool:
        if sts.lane_buffer[lane_id] == cid:
            return True
        agv = self.agv_at(lane_id)
        return agv is not None and agv.current_job is None and agv.carries(cid)

    def transfer_sts_to_lane(self, sts: Sts2, lane_id: str, cid: str) -> str:
        """DISCHARGE: the box onto the waiting AGV, else into the lane buffer. Returns the AGV
        id or ``"BUFFER"``."""
        agv = self._waiting_agv(lane_id, cid)
        if agv is not None:
            agv.load(cid)
            sts.holding = None
            return agv.equipment_id
        if sts.lane_buffer[lane_id] is not None:
            raise ValueError(f"lane {lane_id} buffer holds {sts.lane_buffer[lane_id]}")
        sts.lane_buffer[lane_id] = cid
        sts.holding = None
        return "BUFFER"

    def transfer_lane_to_sts(self, sts: Sts2, lane_id: str, cid: str) -> None:
        """LOAD: the STS takes ``cid`` from the lane buffer or the AGV in the lane."""
        if sts.lane_buffer[lane_id] == cid:
            sts.lane_buffer[lane_id] = None
        else:
            agv = self.agv_at(lane_id)
            if agv is None or not agv.carries(cid):
                raise ValueError(f"{cid} is not in lane {lane_id}")
            agv.unload(cid)
        sts.holding = cid

    def transfer_lane_to_agv(self, agv: LiftAGV, lane_id: str, cid: str) -> None:
        sts = self._sts_of_lane(lane_id)
        if sts.lane_buffer[lane_id] != cid:
            raise ValueError(f"{cid} is not in the buffer of lane {lane_id}")
        sts.lane_buffer[lane_id] = None
        agv.load(cid)

    # --- racks ------------------------------------------------------------------------
    def rack_occupant(self, rack_id: str) -> str | None:
        return self.racks.occupant(rack_id)

    def reserve_rack(self, rack_id: str, eid: str) -> bool:
        """Book an empty rack for a set-down by ``eid`` (AGV or ASC); False if it holds a box
        or is booked by someone else. Taken before the final approach, released on the put
        (or by ``release_racks`` when the job ends early)."""
        holder = self.rack_res.get(rack_id)
        if self.racks.occupant(rack_id) is not None or (holder is not None and holder != eid):
            return False
        self.rack_res[rack_id] = eid
        return True

    def release_racks(self, eid: str) -> None:
        for rack_id in [r for r, h in self.rack_res.items() if h == eid]:
            del self.rack_res[rack_id]

    def _put_rack(self, rack_id: str, cid: str, eid: str) -> None:
        if self.rack_res.get(rack_id) != eid:
            raise ValueError(f"rack {rack_id} not booked by {eid}")
        self.racks.put(rack_id, cid)
        del self.rack_res[rack_id]

    def transfer_agv_to_rack(self, agv: LiftAGV, rack_id: str, cid: str) -> None:
        self._put_rack(rack_id, cid, agv.equipment_id)
        agv.unload(cid)

    def transfer_rack_to_agv(self, agv: LiftAGV, rack_id: str, cid: str) -> None:
        if self.racks.occupant(rack_id) != cid:
            raise ValueError(f"{cid} is not on rack {rack_id}")
        agv.load(cid)
        self.racks.take(rack_id)

    def transfer_rack_to_asc(self, asc: Asc2, rack_id: str, cid: str) -> None:
        if self.racks.occupant(rack_id) != cid:
            raise ValueError(f"{cid} is not on rack {rack_id}")
        self.racks.take(rack_id)
        asc.holding = cid

    def transfer_asc_to_rack(self, asc: Asc2, rack_id: str, cid: str) -> None:
        self._put_rack(rack_id, cid, asc.equipment_id)
        asc.holding = None


def _geometry(b, zone, truck) -> tuple[float, float]:
    """(lo, hi) of a block track in bay units: the transfer racks and the truck head."""
    def bay(y: float) -> float:
        return (y - b.y0_m) / b.bay_pitch_m - 0.5

    lo = min((bay(r.y_m) for r in zone.racks), default=-1.0)
    if truck is not None:
        hi = bay(truck.y_m + (truck.len_m / 2 if truck.len_m > 0 else 10.0))
    else:
        hi = b.bays - 1.0
    return lo, hi
