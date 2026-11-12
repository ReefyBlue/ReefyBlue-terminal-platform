"""C1b Task 4: the dispatcher in rack mode drives the v2 world (``World2`` in-process via the sim
port): lanes, Lift-AGVs with transfer racks, the WS-ASC per block, lane and apron buffers."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import pytest
from terminal_ecs.alarms import AlarmKind
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.orders import Stage
from terminal_ecs.workorders import WorkOrder
from terminal_model.layout import LayoutError
from terminal_model.locations import SlotLoc
from terminal_model.port import EquipmentEvent, EventKind, LinkReason
from terminal_model.types import JobType, State
from terminal_sim.layout import load_layout
from terminal_sim.loc2 import parse_loc2
from terminal_sim.scenario import load_scenario
from terminal_sim.world import make_world

ROOT = Path(__file__).resolve().parents[2]
MV_SMALL = ROOT / "terminal_mv_small.yaml"
MV_SCENARIO = ROOT / "tests" / "e2e" / "fixtures" / "mv_small_baseline.yaml"
STS = ("STS-01", "STS-02", "STS-03")
BLOCKS = ("A", "B", "C", "D")


class Slots:
    """Distinct columns per block (bays 5.., rows 1..8: no reefer row); the tier is ignored in
    rack mode (the ASC sets the box down on top of the column)."""

    def __init__(self) -> None:
        self._n: dict[str, int] = defaultdict(int)

    def next_slot(self, block: str, exclude: str | None = None) -> str | None:
        n = self._n[block]
        self._n[block] += 1
        return f"{block}/{6 + n // 8}/{1 + n % 8}/1"


def make(**kw):
    layout = load_layout(MV_SMALL)
    world = make_world(layout, load_scenario(MV_SCENARIO, layout))
    slots = Slots()
    kw.setdefault("auto_release", True)
    d = Dispatcher(world.port, slots, layout=layout.terminal, **kw)
    return world, d, slots


def run_until(world, d, cond, *, limit: float = 20000.0, step: float = 5.0) -> None:
    t = world.now()
    while not cond():
        assert t < limit, f"timed out at {t}: {d.status()}"
        t += step
        world.run(t)


def discharge(cid: str, sts: str, slot: str, **kw) -> WorkOrder:
    return WorkOrder(cid, JobType.DISCHARGE, sts, slot, **kw)


def column(slot: str) -> tuple[str, int, int]:
    b, bay, row, _ = slot.split("/")
    return b, int(bay) - 1, int(row) - 1


def jobs_for(d: Dispatcher, cid: str) -> list:
    return [lj for jid, lj in d.job_log.items() if lj.job.container_id == cid]


def where(world, cid: str) -> list[str]:
    """Every place the box is (must be exactly one)."""
    out = []
    if world.stack.locate(cid) is not None:
        out.append("stack")
    out += [f"rack:{r}" for r in world.rack_specs if world.racks.occupant(r) == cid]
    out += [f"agv:{a}" for a, agv in world.agvs.items() if agv.carries(cid)]
    for s in world.sts:
        out += [f"lane_buffer:{ln}" for ln, c in s.lane_buffer.items() if c == cid]
        if s.holding == cid:
            out.append(f"sts:{s.equipment_id}")
    out += [f"asc:{e}" for e, eq in world.equipment.items() if e.startswith("ASC") and eq.holding == cid]
    out += [f"vessel:{b}" for b, v in world.vessels.items() if cid in v.loaded]
    return out


# ------------------------------------------------------------------ happy paths

def test_rack_mode_is_detected_from_the_layout():
    world, d, _ = make()
    assert d.rack_mode
    legacy = Dispatcher(world.port, Slots())  # no layout: legacy behaviour
    assert not legacy.rack_mode


def test_happy_path_discharge_20_containers():
    world, d, slots = make()
    orders = [discharge(f"D{i:02d}", STS[i % 3], slots.next_slot(BLOCKS[i % 4])) for i in range(20)]
    d.submit(orders)
    run_until(world, d, lambda: d.status()["open"] == 0)
    s = d.status()
    assert (s["done"], s["failed"], s["replans"], s["stuck"]) == (20, 0, 0, 0)
    for o in orders:
        assert world.stack.locate(o.container_id)[:3] == column(o.yard_slot)
        assert where(world, o.container_id) == ["stack"]
        types = [lj.job.type for lj in jobs_for(d, o.container_id)]
        assert types == [JobType.TRANSPORT, JobType.DISCHARGE, JobType.TRANSPORT, JobType.STACK_IN]
        legs = jobs_for(d, o.container_id)
        assert parse_loc2(legs[1].job.from_loc).berth == "A"  # v2 grammar on every job
        assert legs[2].job.to_loc.startswith("RACK:RK-" + o.yard_slot[0])
        assert legs[3].equipment_id == f"ASC-{o.yard_slot[0]}-WS"
    assert all(world.racks.occupant(r) is None for r in world.rack_specs)
    assert all(a.loaded_container_id is None for a in world.agvs.values())
    # freed AGVs park on the apron buffers, never on a lane or rack point
    world.run(world.now() + 600)
    assert all(a.at_point is None for a in world.agvs.values())
    assert d.alarms() == []


def test_happy_path_load_20_containers():
    world, d, slots = make()
    orders = []
    for i in range(20):
        slot = slots.next_slot(BLOCKS[i % 4])
        b, bay, row, tier = slot.split("/")
        world.yard.occupy(SlotLoc(b, int(bay), int(row), int(tier)), f"L{i:02d}")
        orders.append(WorkOrder(f"L{i:02d}", JobType.LOAD, STS[i % 3], slot))
    d.submit(orders)
    run_until(world, d, lambda: d.status()["open"] == 0)
    s = d.status()
    assert (s["done"], s["failed"], s["replans"], s["stuck"]) == (20, 0, 0, 0)
    for o in orders:
        assert where(world, o.container_id) == ["vessel:A"]
        types = [lj.job.type for lj in jobs_for(d, o.container_id)]
        assert types == [JobType.STACK_OUT, JobType.TRANSPORT, JobType.TRANSPORT, JobType.LOAD]
    assert all(world.racks.occupant(r) is None for r in world.rack_specs)
    # per STS the vessel was loaded in TOS order
    for sts in STS:
        mine = [o.container_id for o in orders if o.sts_id == sts]
        loaded = [c for c in world.vessels["A"].loaded if c in mine]
        assert loaded == mine


def test_reefer_box_goes_to_the_reefer_rack():
    world, d, _ = make()
    d.submit([discharge("R1", "STS-01", "A/6/10/1", reefer=True)])
    run_until(world, d, lambda: d.status()["open"] == 0)
    legs = jobs_for(d, "R1")
    assert legs[2].job.to_loc == "RACK:RK-A-R"
    assert d.status()["done"] == 1


# ------------------------------------------------------------------ racks full: BUFFER_WAIT

def fill_racks(world, block: str, n: int = 5) -> list[str]:
    racks = [f"RK-{block}-{i}" for i in range(1, n + 1)]
    for r in racks:
        world.racks.put(r, f"X-{r}")
    return racks


def test_all_racks_full_agv_waits_in_buffer():
    """Review Focus 1: every rack of the block is taken when the box comes off the crane. The
    AGV waits in a buffer of its STS (BUFFER_WAIT) with the box on board - never in the lane or
    the transfer zone, never dropping the box - and goes on as soon as a rack is free."""
    world, d, _ = make()
    fill_racks(world, "A")
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])
    rec = d._open["D1"]
    run_until(world, d, lambda: rec.stage is Stage.BUFFER_WAIT and rec.current_job_id is None)
    agv_id = rec.agv_id
    agv = world.agvs[agv_id]
    buf_leg = jobs_for(d, "D1")[-1].job
    assert buf_leg.to_loc.startswith("BUFFER:BUF-01-")  # a buffer of its own STS
    world.run(world.now() + 900)  # racks stay full: it keeps waiting
    assert rec.stage is Stage.BUFFER_WAIT and rec.agv_id == agv_id
    assert where(world, "D1") == [f"agv:{agv_id}"]
    assert agv.at_point is None and agv_id not in world.points.values()  # no lane / rack point
    assert not any(lj.job.to_loc.startswith("RACK:") for lj in jobs_for(d, "D1"))
    assert d._free_lanes("STS-01") == ["STS-01-L1", "STS-01-L2", "STS-01-L3"]  # lane given back
    # a rack frees up: the next tick sends the AGV there
    world.racks.take("RK-A-3")
    d.tick()
    assert jobs_for(d, "D1")[-1].job.to_loc == "RACK:RK-A-3"
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert where(world, "D1") == ["stack"]
    s = d.status()
    assert (s["done"], s["failed"], s["replans"]) == (1, 0, 0)
    assert d.alarms() == []


def test_all_racks_full_reblocks_to_an_alternative_block():
    world, d, slots = make()
    fill_racks(world, "A")
    slots._n["B"] = 3
    d.submit([discharge("D1", "STS-01", "A/6/1/1", alternative_blocks=("B",))])
    run_until(world, d, lambda: d.status()["open"] == 0)
    legs = jobs_for(d, "D1")
    assert legs[2].job.to_loc == "RACK:RK-B-1"
    assert legs[3].equipment_id == "ASC-B-WS" and legs[3].job.to_loc == "SLOT:B:5:3"
    assert world.stack.locate("D1")[:3] == ("B", 5, 3)
    assert not any(lj.job.to_loc.startswith("BUFFER:") for lj in legs)
    s = d.status()
    assert (s["done"], s["failed"], s["replans"]) == (1, 0, 1)


def test_rack_taken_behind_the_dispatchers_back_is_retried_not_alarmed():
    """RACK_OCCUPIED is retryable: the AGV's job fails at the rack (box stays on board), the
    dispatcher picks another rack - no replan, no exclusion, no alarm."""
    world, d, _ = make()
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])
    rec = d._open["D1"]
    run_until(world, d, lambda: rec.stage is Stage.TO_RACK and rec.current_job_id is not None, step=0.5)
    assert rec.rack == "RK-A-1"
    world.racks.put("RK-A-1", "FOREIGN")
    run_until(world, d, lambda: d.status()["open"] == 0)
    legs = [lj.job.to_loc for lj in jobs_for(d, "D1")]
    assert legs[2:4] == ["RACK:RK-A-1", "RACK:RK-A-2"]
    s = d.status()
    assert (s["done"], s["failed"], s["replans"], s["needs_release"]) == (1, 0, 0, 0)
    assert where(world, "D1") == ["stack"] and world.racks.occupant("RK-A-1") == "FOREIGN"


def test_load_waits_in_await_asc_until_a_rack_is_free():
    world, d, _ = make()
    fill_racks(world, "A")
    world.yard.occupy(SlotLoc("A", 6, 1, 1), "L1")
    d.submit([WorkOrder("L1", JobType.LOAD, "STS-01", "A/6/1/1")])
    world.run(600)
    rec = d._open["L1"]
    assert rec.stage is Stage.AWAIT_ASC and not jobs_for(d, "L1")
    world.racks.take("RK-A-2")
    d.tick()
    assert jobs_for(d, "L1")[0].job.to_loc == "RACK:RK-A-2"
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert where(world, "L1") == ["vessel:A"]


def test_loads_leave_a_rack_free_for_discharges():
    """STACK_OUTs fill at most all but one rack of a kind: with no AGV to fetch them the WS-ASC
    puts 4 load boxes on the 5 racks of block A and stops; a discharge still finds the fifth."""
    world, d, _ = make()
    for agv in world.agvs.values():
        agv.set_state(State.FAULT)
    for i in range(8):
        world.yard.occupy(SlotLoc("A", 6 + i, 1, 1), f"L{i}")
    d.submit([WorkOrder(f"L{i}", JobType.LOAD, "STS-01", f"A/{6 + i}/1/1") for i in range(8)])
    world.run(3000)
    on_racks = [world.racks.occupant(f"RK-A-{i}") for i in range(1, 6)]
    assert on_racks == ["L0", "L1", "L2", "L3", None]
    assert [d._open[f"L{i}"].stage for i in range(4, 8)] == [Stage.AWAIT_ASC] * 4
    assert d._free_rack("A", False, set()) == "RK-A-5"  # still there for a discharge
    d.submit([discharge("D1", "STS-02", "A/20/1/1")])
    for agv in world.agvs.values():
        agv.set_state(State.IDLE)
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert not any(lj.job.to_loc.startswith("BUFFER:") for lj in jobs_for(d, "D1"))
    assert d.status()["done"] == 9
    assert world.vessels["A"].loaded == [f"L{i}" for i in range(8)]


# ------------------------------------------------------------------ lane buffer, faults

def test_lane_buffer_pickup_when_the_agv_was_not_ready():
    """The AGV in the lane goes down while the crane brings the box: the STS sets it in the lane
    buffer; after the repair the AGV lifts it from the buffer itself and the order goes on."""
    world, d, _ = make()
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])
    rec = d._open["D1"]
    run_until(world, d, lambda: any(lj.job.type is JobType.DISCHARGE for lj in jobs_for(d, "D1")), step=0.5)
    agv = world.agvs[rec.agv_id]
    agv.set_state(State.FAULT)
    run_until(world, d, lambda: rec.stage is Stage.LANE_PICKUP, step=0.5)
    sts = world.equipment["STS-01"]
    assert sts.lane_buffer[rec.lane] == "D1" and where(world, "D1") == [f"lane_buffer:{rec.lane}"]
    assert rec.lane not in d._free_lanes("STS-01")  # the buffered box blocks its lane
    world.run(world.now() + 120)
    agv.set_state(State.IDLE)  # repaired (auto_release re-admits it)
    run_until(world, d, lambda: d.status()["open"] == 0)
    legs = [lj.job for lj in jobs_for(d, "D1")]
    assert [j.type for j in legs] == [JobType.TRANSPORT, JobType.DISCHARGE, JobType.TRANSPORT,
                                      JobType.TRANSPORT, JobType.STACK_IN]
    assert legs[2].from_loc == legs[2].to_loc == legs[1].to_loc  # the pickup in the lane
    assert where(world, "D1") == ["stack"]
    assert sts.extra()["wait_for_agv_s"] == 0
    assert d.status()["failed"] == 0


@pytest.mark.parametrize("in_spreader", [False, True])
def test_asc_fault_during_on_rack_keeps_the_box_and_the_order_continues(in_spreader):
    """Fault of the WS-ASC while the box waits on the rack (before the pick: it stays on the
    rack) or after the pick (it stays in the spreader): after repair and operator release the
    same STACK_IN is re-issued to the same crane and the order completes."""
    world, d, _ = make(auto_release=False)
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])
    rec = d._open["D1"]
    asc = world.equipment["ASC-A-WS"]
    run_until(world, d, lambda: rec.stage is Stage.STACKING, step=0.5)
    if in_spreader:
        run_until(world, d, lambda: asc.holding == "D1", step=0.5)
    asc.set_state(State.FAULT)
    asc.fail_job("FAULT")
    assert rec.stage is Stage.ON_RACK and rec.current_job_id is None and rec.rack == "RK-A-1"
    assert where(world, "D1") == (["asc:ASC-A-WS"] if in_spreader else ["rack:RK-A-1"])
    world.run(world.now() + 300)
    assert rec.stage is Stage.ON_RACK  # nothing re-issued while down
    assert where(world, "D1") == (["asc:ASC-A-WS"] if in_spreader else ["rack:RK-A-1"])
    asc.set_state(State.IDLE)  # repaired
    world.run(world.now() + 60)
    assert rec.stage is Stage.ON_RACK  # needs the operator's release
    assert [a.equipment_id for a in d.alarms()] == ["ASC-A-WS"]
    d.release("ASC-A-WS")
    run_until(world, d, lambda: d.status()["open"] == 0)
    stack_ins = [lj for lj in jobs_for(d, "D1") if lj.job.type is JobType.STACK_IN]
    assert [lj.equipment_id for lj in stack_ins] == ["ASC-A-WS", "ASC-A-WS"]
    assert stack_ins[0].job.to_loc == stack_ins[1].job.to_loc
    assert where(world, "D1") == ["stack"]
    s = d.status()
    assert (s["done"], s["failed"], s["replans"]) == (1, 0, 1)


def test_holding_asc_gets_no_job_for_another_box():
    world, d, _ = make(auto_release=False)
    world.yard.occupy(SlotLoc("A", 9, 1, 1), "L1")
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])
    rec = d._open["D1"]
    asc = world.equipment["ASC-A-WS"]
    run_until(world, d, lambda: asc.holding == "D1", step=0.5)
    asc.set_state(State.FAULT)
    asc.fail_job("FAULT")
    asc.set_state(State.IDLE)
    d.submit([WorkOrder("L1", JobType.LOAD, "STS-02", "A/9/1/1")])  # needs the same crane
    d.release("ASC-A-WS")
    assert jobs_for(d, "L1") == []  # the crane first sets D1 down
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert rec.stage is Stage.DONE and where(world, "L1") == ["vessel:A"]


def _fault(eq) -> None:
    eq.set_state(State.FAULT)
    eq.fail_job("FAULT")


@pytest.mark.parametrize("case", ["sts_discharge", "sts_load", "asc_stack_in", "asc_stack_out"])
def test_crane_fault_after_the_handover_is_not_reissued(case):
    """A crane that faults while hoisting up empty after the set-down fails a job whose box has
    already moved: re-issuing it would pick a box that is no longer there (the vessel model would
    even make a second one). The dispatcher sees the box moved and carries on."""
    world, d, _ = make()
    load = case in ("sts_load", "asc_stack_out")
    cid = "L1" if load else "D1"
    if load:
        world.yard.occupy(SlotLoc("A", 6, 1, 1), cid)
        d.submit([WorkOrder(cid, JobType.LOAD, "STS-01", "A/6/1/1")])
    else:
        d.submit([discharge(cid, "STS-01", "A/6/1/1")])
    sts, asc = world.equipment["STS-01"], world.equipment["ASC-A-WS"]
    eq, moved = {
        "sts_discharge": (sts, lambda: any(a.carries(cid) for a in world.agvs.values())),
        "sts_load": (sts, lambda: cid in world.vessels["A"].loaded),
        "asc_stack_in": (asc, lambda: world.stack.locate(cid) is not None),
        "asc_stack_out": (asc, lambda: world.racks.occupant("RK-A-1") == cid),
    }[case]
    run_until(world, d, lambda: moved() and eq.current_job is not None, step=0.5)
    _fault(eq)
    world.run(world.now() + 60)
    eq.set_state(State.IDLE)
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert where(world, cid) == (["vessel:A"] if load else ["stack"])
    s = d.status()
    assert (s["done"], s["failed"], s["replans"]) == (1, 0, 0)
    job_type = {"sts_discharge": JobType.DISCHARGE, "sts_load": JobType.LOAD,
                "asc_stack_in": JobType.STACK_IN, "asc_stack_out": JobType.STACK_OUT}[case]
    assert sum(lj.job.type is job_type for lj in jobs_for(d, cid)) == 1  # not re-issued
    assert all(c is None for c in sts.lane_buffer.values())


# ------------------------------------------------------------------ link loss (B2d) in rack mode

class LinkPort:
    """The sim port seen through a link that can be cut per device: while cut, the device's
    events are lost, its snapshot reports OFFLINE and it takes no commands."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.down: set[str] = set()
        self.strip: dict[str, set[str]] = {}  # snapshot extra keys the device does not report
        self.frozen: dict[str, object] = {}  # muted device -> position it keeps reporting
        self._subs = []
        inner.subscribe(self._on)

    def _on(self, ev) -> None:
        if ev.equipment_id not in self.down and ev.equipment_id not in self.frozen:
            for cb in list(self._subs):
                cb(ev)

    def _emit(self, ev) -> None:
        for cb in list(self._subs):
            cb(ev)

    def equipment_ids(self):
        return self.inner.equipment_ids()

    def snapshot(self, eid):
        s = self.inner.snapshot(eid)
        if eid in self.strip:
            s = replace(s, extra={k: v for k, v in s.extra.items() if k not in self.strip[eid]})
        if eid in self.frozen:
            s = replace(s, position=self.frozen[eid])
        return replace(s, state=State.OFFLINE) if eid in self.down else s

    def assign_job(self, eid, job) -> bool:
        return eid not in self.down and self.inner.assign_job(eid, job)

    def cancel_job(self, eid) -> bool:
        return eid not in self.down and self.inner.cancel_job(eid)

    def reset(self, eid) -> bool:
        return eid not in self.down and self.inner.reset(eid)

    def subscribe(self, cb) -> None:
        self._subs.append(cb)

    def now(self) -> float:
        return self.inner.now()

    def rack_occupancy(self, block):
        return self.inner.rack_occupancy(block)

    def cut(self, eid: str) -> None:
        self.down.add(eid)
        self._emit(EquipmentEvent(EventKind.STATE_CHANGED, eid, self.now(), reason=LinkReason.LINK_LOST,
                                  new_state=State.OFFLINE))

    def restore(self, eid: str) -> None:
        self.down.discard(eid)
        self._emit(EquipmentEvent(EventKind.STATE_CHANGED, eid, self.now(), reason=LinkReason.LINK_RESTORED,
                                  new_state=self.inner.snapshot(eid).state))


def make_linked(**kw):
    layout = load_layout(MV_SMALL)
    world = make_world(layout, load_scenario(MV_SCENARIO, layout))
    port = LinkPort(world.port)
    kw.setdefault("auto_release", True)
    return world, port, Dispatcher(port, Slots(), layout=layout.terminal, **kw)


def test_link_loss_completed_during_outage_is_reconciled():
    world, port, d = make_linked()
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])
    rec = d._open["D1"]
    run_until(world, d, lambda: rec.stage is Stage.TO_RACK and rec.current_job_id is not None, step=0.5)
    agv_id, job_id = rec.agv_id, rec.current_job_id
    port.cut(agv_id)
    agv = world.agvs[agv_id]
    run_until(world, d, lambda: agv.last_job_id == job_id, step=0.5)
    assert agv.last_job_outcome == "COMPLETED"
    world.run(world.now() + 300)
    assert rec.stage is Stage.TO_RACK and rec.current_job_id == job_id  # no replan while offline
    s = d.status()
    assert (s["offline"], s["replans"]) == (1, 0)
    port.restore(agv_id)
    assert rec.stage in (Stage.ON_RACK, Stage.STACKING)
    run_until(world, d, lambda: d.status()["open"] == 0)
    s = d.status()
    assert (s["done"], s["replans"], s["offline"], s["needs_release"]) == (1, 0, 0, 0)
    assert d.alarms() == [] and where(world, "D1") == ["stack"]


def test_link_loss_fault_during_outage_replans_and_reissues():
    world, port, d = make_linked()
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])
    rec = d._open["D1"]
    sts = world.equipment["STS-01"]
    run_until(world, d, lambda: sts.holding == "D1", step=0.5)
    port.cut("STS-01")
    sts.set_state(State.FAULT)
    sts.fail_job("FAULT")
    world.run(world.now() + 120)
    sts.set_state(State.IDLE)
    world.run(world.now() + 10)
    assert rec.stage is Stage.AT_LANE and d.status()["replans"] == 0
    port.restore("STS-01")
    run_until(world, d, lambda: d.status()["open"] == 0)
    discharges = [lj.job for lj in jobs_for(d, "D1") if lj.job.type is JobType.DISCHARGE]
    assert len(discharges) == 2 and discharges[0].from_loc == discharges[1].from_loc
    s = d.status()
    assert (s["done"], s["failed"], s["replans"], s["offline"]) == (1, 0, 1, 0)
    assert where(world, "D1") == ["stack"]


# ------------------------------------------------------------------ fail closed (review round 1)

def test_rack_mode_refuses_to_start_without_box_locations():
    layout = load_layout(MV_SMALL)
    world = make_world(layout, load_scenario(MV_SCENARIO, layout))
    port = LinkPort(world.port)
    port.strip = {"STS-02": {"holding"}, "AGV-03": {"loaded_container_id"}}
    with pytest.raises(LayoutError, match="holding/lane_buffer/load") as e:
        Dispatcher(port, Slots(), layout=layout.terminal)
    assert "STS-02: holding" in str(e.value) and "AGV-03: loaded_container_id" in str(e.value)

    class NoRacks:  # an equipment port without rack_occupancy
        def __init__(self, inner):
            self.inner = inner

        def equipment_ids(self):
            return self.inner.equipment_ids()

        def snapshot(self, eid):
            return self.inner.snapshot(eid)

        def assign_job(self, eid, job):
            return self.inner.assign_job(eid, job)

        def cancel_job(self, eid):
            return self.inner.cancel_job(eid)

        def reset(self, eid):
            return self.inner.reset(eid)

        def subscribe(self, cb):
            self.inner.subscribe(cb)

        def now(self):
            return self.inner.now()

    with pytest.raises(LayoutError, match="rack_occupancy"):
        Dispatcher(NoRacks(world.port), Slots(), layout=layout.terminal)


def test_missing_holding_after_a_fault_is_unknown_not_reissued():
    """The STS faults with the box in its spreader but its snapshot no longer reports
    ``holding``: the dispatcher cannot tell where the box is, so it re-issues nothing; the job's
    outcome is UNKNOWN (alarm) and the STS needs a release even with auto_release."""
    world, port, d = make_linked(auto_release=True)
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])
    rec = d._open["D1"]
    sts = world.equipment["STS-01"]
    run_until(world, d, lambda: sts.holding == "D1", step=0.5)
    job_id = rec.current_job_id
    port.strip["STS-01"] = {"holding"}
    _fault(sts)
    world.run(world.now() + 60)
    sts.set_state(State.IDLE)
    world.run(world.now() + 300)
    assert [lj.job.job_id for lj in jobs_for(d, "D1") if lj.job.type is JobType.DISCHARGE] == [job_id]
    assert rec.stage is Stage.AT_LANE and rec.current_job_id == job_id
    assert d.unknown_jobs() == {job_id}
    assert {(a.kind, a.equipment_id) for a in d.alarms()} >= {(AlarmKind.UNKNOWN_OUTCOME, "STS-01"),
                                                              (AlarmKind.NEEDS_RELEASE, "STS-01")}
    # the operator checks the crane: the box is still in the spreader -> failed, re-issue
    del port.strip["STS-01"]
    d.resolve_job(job_id, "failed")
    d.release("STS-01")
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert where(world, "D1") == ["stack"]
    s = d.status()
    assert (s["done"], s["failed"], s["replans"], s["unknown_outcomes"]) == (1, 0, 1, 0)


def test_unknown_agv_load_at_discharge_completion_waits_for_resolve():
    """The discharge completes but neither the AGV (no load reported) nor the STS (no
    ``lane_buffer``) shows where the box went: the order waits (UNKNOWN), nothing is issued."""
    world, port, d = make_linked()
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])
    rec = d._open["D1"]
    sts = world.equipment["STS-01"]
    run_until(world, d, lambda: any(lj.job.type is JobType.DISCHARGE for lj in jobs_for(d, "D1")), step=0.5)
    job_id, agv = rec.current_job_id, rec.agv_id
    port.strip = {agv: {"loaded_container_id", "load_a", "load_b"}, "STS-01": {"lane_buffer"}}
    assert d._carrying(agv, "D1") is None
    run_until(world, d, lambda: sts.last_job_id == job_id, step=0.5)
    world.run(world.now() + 120)
    assert rec.stage is Stage.AT_LANE and rec.current_job_id == job_id
    assert d.unknown_jobs() == {job_id} and len(jobs_for(d, "D1")) == 2
    port.strip = {}
    d.resolve_job(job_id, "completed")
    assert rec.stage is Stage.TO_RACK
    d.release("STS-01")
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert where(world, "D1") == ["stack"] and d.alarms() == []


def _parking(world, d, port):
    """Run a discharge; return (agv, park job id) of the freed AGV while its PARK leg runs."""
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])

    def running_park():
        return next((lj for jid, lj in d.job_log.items() if jid.startswith("PARK-")
                     and world.agvs[lj.equipment_id].current_job is lj.job), None)

    run_until(world, d, lambda: running_park() is not None, step=0.5)
    lj = running_park()
    return lj.equipment_id, lj.job.job_id


@pytest.mark.parametrize("outcome", ["failed", "unknown"])
def test_park_ended_during_link_outage_is_parked_again(outcome):
    world, port, d = make_linked()
    agv_id, park = _parking(world, d, port)
    agv = world.agvs[agv_id]
    port.cut(agv_id)
    if outcome == "failed":  # faults mid-park: the JOB_FAILED is lost
        _fault(agv)
        world.run(world.now() + 30)
        agv.set_state(State.IDLE)
    else:  # moved elsewhere by hand: its last job is not the park leg
        agv.cancel_job()
        agv.reset()
        from terminal_model.types import Job
        assert world.port.assign_job(agv_id, Job("MANUAL", JobType.TRANSPORT, "", agv_id, "LANE:STS-02-L1"))
        run_until(world, d, lambda: agv.last_job_id == "MANUAL", step=0.5)
    port.restore(agv_id)
    assert park not in d._job_eq and park not in d._park_jobs
    agv_parks = [jid for jid, lj in d.job_log.items() if jid.startswith("PARK-") and lj.equipment_id == agv_id]
    assert len(agv_parks) == 2  # parked again at once
    run_until(world, d, lambda: d.status()["open"] == 0 and agv.current_job is None)
    assert agv.at_point is None and agv_id in d._buffer_agv.values()
    assert d.alarms() == [] and d.unknown_jobs() == frozenset()


def test_stalled_park_is_cancelled_by_the_watchdog_and_parked_again():
    world, port, d = make_linked(watchdog_s=5.0)
    agv_id, park = _parking(world, d, port)
    port.frozen[agv_id] = port.inner.snapshot(agv_id).position  # no progress seen
    world.run(world.now() + 10)
    d.tick()  # watchdog: cancel + reset, and the same tick parks it again
    assert park not in d._park_jobs and park not in d._job_eq
    assert world.agvs[agv_id].current_job.job_id != park
    del port.frozen[agv_id]
    agv_parks = [jid for jid, lj in d.job_log.items() if jid.startswith("PARK-") and lj.equipment_id == agv_id]
    assert len(agv_parks) == 2
    run_until(world, d, lambda: d.status()["open"] == 0 and world.agvs[agv_id].current_job is None)
    assert world.agvs[agv_id].at_point is None


_NO_LAST_JOB = {"last_job_id", "last_job_outcome"}  # the device does not say what its last job was


@pytest.mark.parametrize("case", ["sts_discharge", "asc_stack_in"])
def test_auto_release_reconcile_of_an_unknown_outcome_checks_where_the_box_is(case):
    """The crane sets the box down during a link outage and comes back without reporting its last
    job: the outcome is unknown and auto_release resolves it as failed - but in rack mode that
    still looks where the box is first. It moved, so nothing is re-issued and the order goes on."""
    world, port, d = make_linked(auto_release=True)
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])
    rec = d._open["D1"]
    if case == "sts_discharge":
        eid, jtype = "STS-01", JobType.DISCHARGE
        eq = world.equipment[eid]
        run_until(world, d, lambda: eq.holding == "D1", step=0.5)
        moved = lambda: any(a.carries("D1") for a in world.agvs.values())
    else:
        eid, jtype = "ASC-A-WS", JobType.STACK_IN
        eq = world.equipment[eid]
        run_until(world, d, lambda: eq.holding == "D1", step=0.5)
        moved = lambda: world.stack.locate("D1") is not None
    port.cut(eid)
    port.strip[eid] = set(_NO_LAST_JOB)
    run_until(world, d, lambda: moved() and eq.current_job is None, step=0.5)
    port.restore(eid)
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert sum(lj.job.type is jtype for lj in jobs_for(d, "D1")) == 1  # not re-issued
    assert where(world, "D1") == ["stack"]
    s = d.status()
    assert (s["done"], s["failed"], s["replans"], s["unknown_outcomes"]) == (1, 0, 0, 0)
    assert d.alarms() == []
    assert rec.stage is Stage.DONE


def test_auto_release_reconcile_without_the_box_location_stays_unknown():
    """Same, but the STS does not report ``holding`` either: the box cannot be located, so the
    automatic verdict does not re-issue; UNKNOWN alarm and release until the operator decides."""
    world, port, d = make_linked(auto_release=True)
    d.submit([discharge("D1", "STS-01", "A/6/1/1")])
    rec = d._open["D1"]
    sts = world.equipment["STS-01"]
    run_until(world, d, lambda: sts.holding == "D1", step=0.5)
    job_id = rec.current_job_id
    port.cut("STS-01")
    port.strip["STS-01"] = _NO_LAST_JOB | {"holding"}
    run_until(world, d, lambda: sts.current_job is None, step=0.5)
    port.restore("STS-01")
    world.run(world.now() + 300)
    assert rec.stage is Stage.AT_LANE and rec.current_job_id == job_id
    assert d.unknown_jobs() == {job_id}
    assert {(a.kind, a.equipment_id) for a in d.alarms()} >= {(AlarmKind.UNKNOWN_OUTCOME, "STS-01"),
                                                              (AlarmKind.NEEDS_RELEASE, "STS-01")}
    assert [lj.job.job_id for lj in jobs_for(d, "D1") if lj.job.type is JobType.DISCHARGE] == [job_id]
    del port.strip["STS-01"]
    d.resolve_job(job_id, "completed")  # the operator saw the box on the AGV
    d.release("STS-01")
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert where(world, "D1") == ["stack"] and d.alarms() == []
