"""ASC pair on one block track: BlockTrack arbitration, Asc2 jobs, never closer than the gap."""
from __future__ import annotations

import random

import pytest
import salabim
from terminal_model.port import EventKind
from terminal_model.types import Job, JobType, State
from terminal_sim.equipment.asc2 import Asc2
from terminal_sim.equipment.block_track import LS, WS, BlockTrack


# --- BlockTrack ------------------------------------------------------------------------------
def _track():
    t = BlockTrack(40, 2, lo=-5.0, hi=42.0)
    calls = []
    t.register("W", WS, 0.0, on_yield=lambda: calls.append("W"))
    t.register("L", LS, 39.0, on_yield=lambda: calls.append("L"))
    return t, calls


def test_claim_keeps_the_gap_and_remembers_the_wish():
    t, _ = _track()
    assert t.claim("W", 0, 30, 10.0)
    assert t.extent("W") == (0, 30)
    assert not t.claim("L", 39, 31, 10.0)  # 31 - 2 < 30
    assert t.claim("L", 39, 32, 10.0)
    t.release("W", at=30)
    t.release("L", at=32)
    assert t.separation() == 2
    assert not t.claim("W", 30, 31, 0.0)
    assert t.wanted["W"] == (30, 31)


def test_yield_request_targets_and_callbacks():
    t, calls = _track()
    assert not t.claim("W", 0, 38.5, 0.0)
    assert t.yield_request("W", "L") == pytest.approx(40.5)
    assert t.pending_yield("L") == pytest.approx(40.5) and calls == ["L"]
    t.release("W", at=0)
    t.clear_yield("L")
    # LS wants bay 1: WS must go to -1
    assert not t.claim("L", 39, 1, 0.0)
    assert t.yield_request("L", "W") == pytest.approx(-1.0)
    with pytest.raises(ValueError):
        t.claim("L", 39, 50, 0.0)


def test_register_rejects_overlap_and_duplicate_roles():
    t = BlockTrack(40, 2)
    t.register("W", WS, 10)
    with pytest.raises(ValueError):
        t.register("X", WS, 30)
    with pytest.raises(ValueError):
        t.register("L", LS, 11)


# --- Asc2 jobs --------------------------------------------------------------------------------
def _job(jid, jtype, cid, frm, to):
    return Job(jid, jtype, cid, frm, to)


def test_roles_accept_their_jobs(world2):
    ws, ls = world2.equipment["ASC-A-WS"], world2.equipment["ASC-A-LS"]
    assert isinstance(ws, Asc2) and ws.role == "WS" and ls.partner == "ASC-A-WS"
    stack_in = _job("J", JobType.STACK_IN, "C1", "RACK:RK-A-1", "SLOT:A:3:4")
    assert ws.accepts(stack_in) and not ls.accepts(stack_in)
    move = _job("J", JobType.STACK_MOVE, "C1", "SLOT:A:3:4", "SLOT:A:5:4")
    assert ws.accepts(move) and ls.accepts(move)
    truck = _job("J", JobType.TRUCK_IN, "C1", "TRUCK:A:0", "SLOT:A:39:0")
    assert ls.accepts(truck) and not ws.accepts(truck)
    assert not ws.accepts(_job("J", JobType.STACK_IN, "C1", "RACK:RK-B-1", "SLOT:A:3:4"))
    assert not ws.accepts(_job("J", JobType.STACK_IN, "C1", "RACK:RK-A-1", "SLOT:A:40:0"))
    assert not ls.accepts(_job("J", JobType.TRUCK_IN, "C1", "TRUCK:A:4", "SLOT:A:39:0"))


def test_stack_in_out_and_move(world2, rec):
    r = rec(world2)
    world2.racks.put("RK-A-1", "C1")
    assert world2.port.assign_job("ASC-A-WS", _job("J1", JobType.STACK_IN, "C1", "RACK:RK-A-1", "SLOT:A:3:4"))
    world2.run(1000)
    assert r.done("ASC-A-WS", "J1") is not None
    assert world2.stack.locate("C1") == ("A", 3, 4, 0) and world2.racks.occupant("RK-A-1") is None
    ex = world2.port.snapshot("ASC-A-WS").extra
    assert (ex["bay"], ex["row"], ex["tier"], ex["role"], ex["partner"]) == (3, 4, 0, "WS", "ASC-A-LS")
    world2.stack.put("A", 3, 4, "C2")  # C2 on top of C1
    world2.port.assign_job("ASC-A-WS", _job("J2", JobType.STACK_OUT, "C1", "SLOT:A:3:4", "RACK:RK-A-2"))
    world2.run(1001)
    assert r.failed("ASC-A-WS", "J2") == "NOT_ON_TOP"
    world2.port.assign_job("ASC-A-LS", _job("J3", JobType.STACK_MOVE, "C2", "SLOT:A:3:4", "SLOT:A:6:4"))
    world2.run(3000)
    assert r.done("ASC-A-LS", "J3") is not None
    assert world2.stack.locate("C2") == ("A", 6, 4, 0)
    world2.port.assign_job("ASC-A-WS", _job("J4", JobType.STACK_OUT, "C1", "SLOT:A:3:4:0", "RACK:RK-A-2"))
    world2.run(5000)
    assert r.done("ASC-A-WS", "J4") is not None
    assert world2.racks.occupant("RK-A-2") == "C1" and world2.stack.locate("C1") is None


def test_stack_in_cycle_time_is_plausible(world2, rec):
    r = rec(world2)
    world2.racks.put("RK-A-1", "C1")
    world2.port.assign_job("ASC-A-WS", _job("J1", JobType.STACK_IN, "C1", "RACK:RK-A-1", "SLOT:A:20:5"))
    world2.run(1000)
    assert 100 < r.done("ASC-A-WS", "J1") < 300


def test_stack_in_fails_without_the_box_on_the_rack(world2, rec):
    r = rec(world2)
    world2.port.assign_job("ASC-A-WS", _job("J1", JobType.STACK_IN, "C1", "RACK:RK-A-1", "SLOT:A:3:4"))
    world2.run(10)
    assert r.failed("ASC-A-WS", "J1") == "RACK_EMPTY"


def test_truck_in_out_take_the_fixed_cycle(world2, rec):
    r = rec(world2)
    world2.port.assign_job("ASC-A-LS", _job("J1", JobType.TRUCK_IN, "C1", "TRUCK:A:1", "SLOT:A:39:2"))
    world2.run(1000)
    assert r.done("ASC-A-LS", "J1") == pytest.approx(180.0)
    assert world2.stack.locate("C1") == ("A", 39, 2, 0)
    t0 = world2.now()
    world2.port.assign_job("ASC-A-LS", _job("J2", JobType.TRUCK_OUT, "C1", "SLOT:A:39:2", "TRUCK:A:3"))
    world2.run(t0 + 1000)
    assert r.done("ASC-A-LS", "J2") - t0 == pytest.approx(180.0)
    assert world2.stack.locate("C1") is None


def test_ls_yields_to_ws_needing_the_landside_bays(world2, rec):
    r = rec(world2)
    ws, ls = world2.equipment["ASC-A-WS"], world2.equipment["ASC-A-LS"]
    world2.racks.put("RK-A-1", "C1")
    world2.port.assign_job("ASC-A-WS", _job("J1", JobType.STACK_IN, "C1", "RACK:RK-A-1", "SLOT:A:39:0"))
    states = []
    world2.port.subscribe(lambda e: states.append((e.equipment_id, e.new_state))
                          if e.kind is EventKind.STATE_CHANGED else None)
    world2.run(2000)
    assert r.done("ASC-A-WS", "J1") is not None
    assert ls.gantry_bay >= 41.0 - 1e-9  # it moved out of the way (BUSY without a job)
    assert ("ASC-A-LS", State.BUSY) in states and ls.state is State.IDLE
    assert ws.gantry_bay == 39.0


def test_asc_pair_never_closer_than_min_gap(make_world2):
    """Review Focus 3: a random job stream for both cranes of block A (seeded): every
    sample keeps ``asc_min_gap_bays``, nothing deadlocks, every job completes."""
    w = make_world2(n_agv=1)
    ws, ls = w.equipment["ASC-A-WS"], w.equipment["ASC-A-LS"]
    track = w.tracks["A"]
    gap = track.gap
    block = w.terminal.block("A")
    rng = random.Random(7)
    racks = [r.id for r in w.terminal.transfer_zone("A").racks if not r.reefer]
    busy_cols: dict[str, set] = {"ASC-A-WS": set(), "ASC-A-LS": set()}
    outcome = {"done": 0, "failed": []}
    n = [0]

    def on_event(e):
        if e.kind is EventKind.JOB_COMPLETED and e.equipment_id in busy_cols:
            outcome["done"] += 1
        elif e.kind is EventKind.JOB_FAILED and e.equipment_id in busy_cols:
            outcome["failed"].append((e.equipment_id, e.reason))

    w.port.subscribe(on_event)

    def taken():
        return set().union(*busy_cols.values())

    def free_col(rows=range(block.rows - 1)):
        cols = [(b, r) for b in range(block.bays) for r in rows
                if w.stack.column_height("A", b, r) < block.tiers and (b, r) not in taken()]
        return rng.choice(cols)

    def top_box():
        tops = [(b, r, w.stack.top("A", b, r)) for b in range(block.bays) for r in range(block.rows)
                if w.stack.top("A", b, r) is not None and (b, r) not in taken()]
        return rng.choice(tops) if tops else None

    def next_job(asc):
        n[0] += 1
        jid = f"J{n[0]}"
        busy_cols[asc.equipment_id] = set()
        if asc.role == "WS":
            kind = rng.choice(["IN", "IN", "OUT", "MOVE"])
            if kind == "IN":
                rack = rng.choice(racks)
                if w.racks.occupant(rack) is None:
                    w.racks.put(rack, f"B{n[0]}")
                cid = w.racks.occupant(rack)
                bay, row = free_col()
                busy_cols[asc.equipment_id] = {(bay, row)}
                return Job(jid, JobType.STACK_IN, cid, f"RACK:{rack}", f"SLOT:A:{bay}:{row}")
            top = top_box()
            if top is None:
                return None
            bay, row, cid = top
            if kind == "OUT":
                rack = next((k for k in racks if w.racks.occupant(k) is None), None)
                if rack is None:
                    w.racks.take(racks[0])
                    rack = racks[0]
                busy_cols[asc.equipment_id] = {(bay, row)}
                return Job(jid, JobType.STACK_OUT, cid, f"SLOT:A:{bay}:{row}", f"RACK:{rack}")
        else:
            kind = rng.choice(["MOVE", "MOVE", "TIN", "TOUT"])
            if kind == "TIN":
                bay, row = free_col()
                busy_cols[asc.equipment_id] = {(bay, row)}
                return Job(jid, JobType.TRUCK_IN, f"T{n[0]}", f"TRUCK:A:{rng.randrange(4)}",
                           f"SLOT:A:{bay}:{row}")
            top = top_box()
            if top is None:
                return None
            bay, row, cid = top
            if kind == "TOUT":
                busy_cols[asc.equipment_id] = {(bay, row)}
                return Job(jid, JobType.TRUCK_OUT, cid, f"SLOT:A:{bay}:{row}",
                           f"TRUCK:A:{rng.randrange(4)}")
        top = top_box()
        if top is None:
            return None
        bay, row, cid = top
        busy_cols[asc.equipment_id] = {(bay, row)}
        dst = free_col()
        busy_cols[asc.equipment_id].add(dst)
        return Job(jid, JobType.STACK_MOVE, cid, f"SLOT:A:{bay}:{row}", f"SLOT:A:{dst[0]}:{dst[1]}")

    seps = []

    class Monitor(salabim.Component):
        def process(self):
            while True:
                seps.append(ls.gantry_bay - ws.gantry_bay)
                assert track.separation() >= gap - 1e-9
                yield self.hold(0.5)

    Monitor(env=w.env)
    assigned = 0
    t = 0.0
    while assigned < 220 and t < 400_000:
        t += 5.0
        w.run(t)
        for asc in (ws, ls):
            if asc.state is State.IDLE and asc.current_job is None:
                job = next_job(asc)
                if job is not None and w.port.assign_job(asc.equipment_id, job):
                    assigned += 1
    t_end = t + 5000
    while (ws.current_job or ls.current_job) and t < t_end:
        t += 5.0
        w.run(t)
    assert assigned >= 200
    assert outcome["failed"] == []
    assert outcome["done"] == assigned  # no deadlock: everything finished
    assert min(seps) >= gap - 1e-9
    assert len(seps) > 1000
