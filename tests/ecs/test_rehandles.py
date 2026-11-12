"""C1b Task 5: digging in rack mode. A LOAD (or TRUCK_OUT) whose box is not on top of its column
first gets ``STACK_MOVE`` rehandles of the boxes above it (top first), issued to the block's
LS-ASC, each to a free column in the same block; the WS-ASC's ``STACK_OUT`` follows only after
all of them. The ECS works from its own column model (order slots, ``register_stack``, and the
outcomes of its own jobs)."""
from __future__ import annotations

from terminal_ecs.alarms import AlarmKind
from terminal_ecs.orders import Stage
from terminal_ecs.workorders import WorkOrder
from terminal_model.locations import SlotLoc
from terminal_model.types import JobType, State
from terminal_sim.loc2 import parse_loc2
from test_rack_mode import make, run_until, where

BLOCK = "A"
TIERS = 5
REEFER_ROW = 9  # 0-based reefer row of the mv_small blocks


def put(world, cid: str, block: str, bay: int, row: int) -> str:
    """Stack ``cid`` on top of the 1-based column (bay, row) in the sim; its 1-based yard slot."""
    tier = world.stack.column_height(block, bay - 1, row - 1) + 1
    world.yard.occupy(SlotLoc(block, bay, row, tier), cid)
    return f"{block}/{bay}/{row}/{tier}"


def load(cid: str, slot: str, sts: str = "STS-01", **kw) -> WorkOrder:
    return WorkOrder(cid, JobType.LOAD, sts, slot, **kw)


def order_jobs(d, cid: str) -> list:
    """The jobs the dispatcher issued for ``cid``'s order (job ids start with the order's box)."""
    return [lj for jid, lj in d.job_log.items() if jid.startswith(f"{cid}-")]


def col_of(loc: str) -> tuple[str, int, int]:
    s = parse_loc2(loc)
    return s.block, s.bay, s.row


def record_assignments(world):
    """Wrap the port's ``assign_job``: (job, equipment, sim column height of the target) per job."""
    seen = []
    real = world.port.assign_job

    def spy(eid, job):
        h = None
        if job.type is JobType.STACK_MOVE:
            h = world.stack.column_height(*col_of(job.to_loc))
        seen.append((job, eid, h))
        return real(eid, job)

    world.port.assign_job = spy
    return seen


def test_load_under_three_boxes_generates_three_rehandles():
    """Review Focus 2."""
    world, d, _ = make()
    slot = put(world, "L0", BLOCK, 11, 3)
    above = {c: put(world, c, BLOCK, 11, 3) for c in ("X1", "X2", "X3")}
    d.register_stack(above)
    d.submit([load("L0", slot)])
    run_until(world, d, lambda: d.status()["open"] == 0)
    s = d.status()
    assert (s["done"], s["failed"], s["replans"]) == (1, 0, 0)
    jobs = order_jobs(d, "L0")
    types = [lj.job.type for lj in jobs]
    assert types == [JobType.STACK_MOVE] * 3 + [JobType.STACK_OUT, JobType.TRANSPORT, JobType.TRANSPORT,
                                                 JobType.LOAD]
    moves = jobs[:3]
    assert [lj.job.container_id for lj in moves] == ["X3", "X2", "X1"]  # top first
    for lj in moves:
        assert lj.equipment_id == "ASC-A-LS"
        assert col_of(lj.job.from_loc) == (BLOCK, 10, 2)
        b, bay, row = col_of(lj.job.to_loc)
        assert b == BLOCK and (bay, row) != (10, 2) and row != REEFER_ROW
    assert jobs[3].equipment_id == "ASC-A-WS"
    assert d.rehandles("L0") == 3
    assert where(world, "L0") == ["vessel:A"]
    for c in above:
        loc = world.stack.locate(c)
        assert loc is not None and loc[0] == BLOCK and loc[1:3] != (10, 2)
    assert d.alarms() == []


def test_rehandle_targets_are_free_columns_same_block():
    world, d, _ = make()
    # The neighbourhood of the dug columns is full (and known to the ECS): targets must be elsewhere.
    full = {}
    for bay in range(9, 14):
        for row in range(1, 11):
            if (bay, row) in ((11, 4), (11, 10)):
                continue
            for t in range(TIERS):
                full[f"F{bay}-{row}-{t}"] = put(world, f"F{bay}-{row}-{t}", BLOCK, bay, row)
    d.register_stack(full)
    dry = put(world, "L1", BLOCK, 11, 4)  # dry box under two dry boxes
    reefer = put(world, "R1", BLOCK, 11, 10)  # reefer row: reefer box under two reefers
    above = {c: put(world, c, BLOCK, 11, 4) for c in ("X1", "X2")}
    above |= {c: put(world, c, BLOCK, 11, 10) for c in ("Y1", "Y2")}
    d.register_stack(above)
    seen = record_assignments(world)
    d.submit([load("L1", dry), load("R1", reefer, "STS-02", reefer=True)])
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert (d.status()["done"], d.status()["failed"]) == (2, 0)
    moves = [(job, eid, h) for job, eid, h in seen if job.type is JobType.STACK_MOVE]
    assert sorted(job.container_id for job, _, _ in moves) == ["X1", "X2", "Y1", "Y2"]
    for job, eid, height in moves:
        assert eid == "ASC-A-LS"
        src, dst = col_of(job.from_loc), col_of(job.to_loc)
        assert dst[0] == src[0] == BLOCK  # same block
        assert dst != src  # never the column being dug
        assert height < TIERS  # free when issued
        assert (dst[2] == REEFER_ROW) == job.container_id.startswith("Y")  # reefer rows only for reefers
        assert not (8 <= dst[1] <= 12)  # not one of the full columns
    assert d.rehandles("L1") == 2 and d.rehandles("R1") == 2
    assert d.alarms() == []


def test_rehandled_load_boxes_are_loaded_from_their_new_column():
    """The boxes above are themselves open LOAD orders: they move with their slot and are loaded
    from where the LS-ASC put them."""
    world, d, _ = make()
    slots = {c: put(world, c, BLOCK, 21, 6) for c in ("L0", "L1", "L2")}
    d.submit([load("L0", slots["L0"]), load("L1", slots["L1"], "STS-02"), load("L2", slots["L2"], "STS-03")])
    run_until(world, d, lambda: d.status()["open"] == 0)
    s = d.status()
    assert (s["done"], s["failed"]) == (3, 0)
    # L2 (on top) is dug away first; L1 is then on top and may leave by itself (no rehandle).
    moved = {lj.job.container_id: lj for lj in order_jobs(d, "L0") if lj.job.type is JobType.STACK_MOVE}
    assert "L2" in moved and d.rehandles("L0") == len(moved)
    for c in ("L1", "L2"):
        (out,) = [lj for lj in order_jobs(d, c) if lj.job.type is JobType.STACK_OUT]
        src = moved[c].job.to_loc if c in moved else "SLOT:A:20:5"
        assert out.job.from_loc == src  # loaded from the column it was moved to
        assert where(world, c) == ["vessel:A"]
    assert where(world, "L0") == ["vessel:A"]
    assert d.alarms() == []


def test_no_free_column_in_the_block_the_load_waits():
    world, d, _ = make()
    slot = put(world, "L0", BLOCK, 20, 5)
    stack = {"X1": put(world, "X1", BLOCK, 20, 5)}
    for bay in range(1, 41):
        for row in range(1, 10):  # every dry column full (the reefer row takes no dry box)
            while world.stack.column_height(BLOCK, bay - 1, row - 1) < TIERS and (bay, row) != (20, 5):
                cid = f"F{bay}-{row}-{world.stack.column_height(BLOCK, bay - 1, row - 1)}"
                stack[cid] = put(world, cid, BLOCK, bay, row)
    d.register_stack(stack)
    d.submit([load("L0", slot)])
    world.run(world.now() + 1500)
    rec = d._open["L0"]
    assert rec.stage is Stage.AWAIT_ASC and rec.current_job_id is None
    assert order_jobs(d, "L0") == [] and d.alarms() == []
    # A truck takes a box away: the column it leaves is the only free one.
    d.submit([WorkOrder("F33-2-4", JobType.TRUCK_OUT, "", stack["F33-2-4"])])
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert d.status()["failed"] == 0
    move = next(lj for lj in order_jobs(d, "L0") if lj.job.type is JobType.STACK_MOVE)
    assert col_of(move.job.to_loc) == (BLOCK, 32, 1)
    assert where(world, "L0") == ["vessel:A"]


def _fill_target_of_moves(world, n: int):
    """The first ``n`` STACK_MOVE targets fill up in the sim just before the job reaches the crane
    (boxes the ECS does not know about)."""
    real = world.port.assign_job
    count = [0]

    def spy(eid, job):
        if job.type is JobType.STACK_MOVE and count[0] < n:
            count[0] += 1
            b, bay, row = col_of(job.to_loc)
            while world.stack.column_height(b, bay, row) < TIERS:
                world.stack.put(b, bay, row, f"U{count[0]}-{world.stack.column_height(b, bay, row)}")
        return real(eid, job)

    world.port.assign_job = spy


def test_rehandle_column_full_repicks_the_target_once():
    world, d, _ = make()
    slot = put(world, "L0", BLOCK, 20, 5)
    d.register_stack({"X1": put(world, "X1", BLOCK, 20, 5)})
    _fill_target_of_moves(world, 1)
    d.submit([load("L0", slot)])
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert (d.status()["done"], d.status()["failed"]) == (1, 0)
    moves = [lj for lj in order_jobs(d, "L0") if lj.job.type is JobType.STACK_MOVE]
    assert len(moves) == 2 and moves[0].job.to_loc != moves[1].job.to_loc
    assert d.rehandles("L0") == 1
    assert world.stack.locate("X1")[1:3] == col_of(moves[1].job.to_loc)[1:]
    assert d.alarms() == []


def test_rehandle_failing_twice_is_an_unknown_outcome_until_resolved():
    world, d, _ = make()
    slot = put(world, "L0", BLOCK, 20, 5)
    d.register_stack({"X1": put(world, "X1", BLOCK, 20, 5)})
    _fill_target_of_moves(world, 2)
    d.submit([load("L0", slot)])
    world.run(world.now() + 1500)
    moves = [lj for lj in order_jobs(d, "L0") if lj.job.type is JobType.STACK_MOVE]
    assert len(moves) == 2  # re-picked once, then no more
    job_id = moves[1].job.job_id
    assert d.unknown_jobs() == {job_id}
    assert (AlarmKind.UNKNOWN_OUTCOME, job_id) in {(a.kind, a.job_id) for a in d.alarms()}
    assert d.status()["failed"] == 0 and world.stack.locate("X1")[1:3] == (19, 4)
    # The operator says it failed: the order digs again, avoiding the two full columns.
    d.resolve_job(job_id, "failed")
    d.release("ASC-A-LS")
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert (d.status()["done"], d.status()["failed"]) == (1, 0)
    moves = [lj for lj in order_jobs(d, "L0") if lj.job.type is JobType.STACK_MOVE]
    assert len(moves) == 3 and len({lj.job.to_loc for lj in moves}) == 3
    assert where(world, "L0") == ["vessel:A"]


def test_ls_fault_with_the_box_in_the_spreader_reissues_the_move_without_pick():
    world, d, _ = make()
    slot = put(world, "L0", BLOCK, 20, 5)
    d.register_stack({"X1": put(world, "X1", BLOCK, 20, 5)})
    d.submit([load("L0", slot)])
    ls = world.equipment["ASC-A-LS"]
    rec = d._open["L0"]
    run_until(world, d, lambda: ls.holding == "X1", step=0.5)
    ls.set_state(State.FAULT)
    ls.fail_job("FAULT")
    assert rec.stage is Stage.AWAIT_ASC and rec.current_job_id is None
    world.run(world.now() + 30)
    assert where(world, "X1") == ["asc:ASC-A-LS"]
    ls.set_state(State.IDLE)  # repaired; auto_release re-admits it
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert (d.status()["done"], d.status()["failed"]) == (1, 0)
    assert d.rehandles("L0") == 1
    moves = [lj for lj in order_jobs(d, "L0") if lj.job.type is JobType.STACK_MOVE]
    assert [lj.equipment_id for lj in moves] == ["ASC-A-LS", "ASC-A-LS"]
    assert world.stack.locate("X1") is not None and world.stack.locate("X1")[1:3] != (19, 4)
    assert where(world, "L0") == ["vessel:A"]


def test_a_buried_head_load_keeps_a_rack_against_non_head_loads():
    """While the FIFO-head LOAD of an STS is dug out, later loads of the same block (on top of
    their columns) must not take every rack under the load cap: their AGVs cannot get a lane while
    discharges of the same STS wait in the lanes for the head, so the head would never get a rack."""
    world, d, slots = make()
    head = put(world, "H", BLOCK, 20, 5)
    d.register_stack({c: put(world, c, BLOCK, 20, 5) for c in ("X1", "X2")})
    later = [load(f"N{i}", put(world, f"N{i}", BLOCK, 25 + i, 3)) for i in range(4)]
    d.submit([load("H", head),
              WorkOrder("D1", JobType.DISCHARGE, "STS-01", slots.next_slot("B")),
              WorkOrder("D2", JobType.DISCHARGE, "STS-01", slots.next_slot("B")),
              *later])
    run_until(world, d, lambda: d.status()["open"] == 0)
    assert (d.status()["done"], d.status()["failed"]) == (7, 0)
    assert d.rehandles("H") == 2
    loaded = [lj.job.container_id for lj in d.job_log.values() if lj.job.type is JobType.LOAD]
    assert loaded == ["H", "N0", "N1", "N2", "N3"]  # TOS order per STS
