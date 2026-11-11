"""Bounded finished-order retention (``prune_done``) and the container change feed
(``containers_since``) of the Dispatcher (Plan B2c Task 1)."""

from __future__ import annotations

import pytest
from fake_port import FakePort, FakeSlots, make_dispatcher, make_port
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.orders import ContainerView
from terminal_ecs.workorders import WorkOrder, WorkOrderError
from terminal_model.types import JobType, State


def _discharge(cid: str, i: int) -> WorkOrder:
    return WorkOrder(cid, JobType.DISCHARGE, "STS-01", f"A/{1 + i // 16}/{1 + (i // 4) % 4}/{1 + i % 4}")


def _load(cid: str, slot: str) -> WorkOrder:
    return WorkOrder(cid, JobType.LOAD, "STS-01", slot)


def _finish(port: FakePort, d: Dispatcher, n: int) -> None:
    """Run the equipment until ``n`` orders are finished (done or failed): every STACK_OUT
    fails with SLOT_EMPTY (the LOAD order fails), every other job completes."""
    while len(d.completed) + d.status()["failed"] < n:
        busy = [eid for eid in port.equipment_ids() if port.snapshot(eid).state is State.BUSY]
        assert busy, "no progress"
        eid = busy[0]
        if d.jobs[port.snapshot(eid).current_job_id].type is JobType.STACK_OUT:
            port.fail(eid, "SLOT_EMPTY")
        else:
            port.complete(eid)


def _setup(n_orders: int = 30, n_loads: int = 2) -> tuple[FakePort, Dispatcher]:
    port = make_port(n_agv=4)
    d = make_dispatcher(port, FakeSlots())
    loads = [_load(f"L{i}", f"A/9/1/{1 + i}") for i in range(n_loads)]
    d.submit(loads + [_discharge(f"C{i:02d}", i) for i in range(n_orders - n_loads)])
    return port, d


def test_prune_keeps_open_and_latest_finished():
    port, d = _setup()
    _finish(port, d, 25)
    before = d.status()
    assert before["failed"] == 2 and before["done"] == 23 and before["open"] == 5

    assert d.prune_done(keep=10) == 15
    after = d.status()
    assert (after["done"], after["failed"], after["open"]) == (23, 2, 5)  # cumulative counters
    assert len(d.completed) == 10

    nxt, items = d.containers_since(0)
    assert all(isinstance(v, ContainerView) for v in items)
    states = [v.state for v in items]
    assert states.count("open") == 5 and states.count("done") == 10 and states.count("failed") == 0
    assert [v.seq for v in items] == sorted(v.seq for v in items) and nxt == items[-1].seq
    retained = {v.container_id for v in items if v.state == "done"}
    assert retained == set(d.completed)
    # every job of a pruned order left the job log; retained ones are still there
    logged = {lj.job.container_id for lj in d.job_log.values()}
    assert logged <= {v.container_id for v in items}
    assert retained <= logged

    pruned = "L0"  # failed first, so the oldest finished record
    assert not d.is_known(pruned)
    with pytest.raises(WorkOrderError) as exc:
        d.submit([_discharge(min(retained), 60)])
    assert exc.value.code == "DUPLICATE_CONTAINER"
    open_id = next(v.container_id for v in items if v.state == "open")
    with pytest.raises(WorkOrderError):
        d.submit([_discharge(open_id, 61)])
    d.submit([_discharge(pruned, 62)])  # accepted again
    assert d.is_known(pruned) and d.status()["open"] == 6


def test_prune_is_noop_below_keep():
    port, d = _setup(n_orders=6, n_loads=0)
    _finish(port, d, 4)
    assert d.prune_done(keep=10) == 0
    assert len(d.completed) == 4 and len(d.containers_since(0)[1]) == 6


def test_container_view_fields():
    port, d = _setup(n_orders=3, n_loads=1)
    _, items = d.containers_since(0)
    assert [(v.container_id, v.state, v.job_type) for v in items] == [
        ("L0", "open", "LOAD"), ("C00", "open", "DISCHARGE"), ("C01", "open", "DISCHARGE"),
    ]
    assert items[1].yard_slot == "A/1/1/1"
    _finish(port, d, 1)
    _, items = d.containers_since(0)
    assert [(v.container_id, v.state) for v in items][-1] == ("L0", "failed")


def test_containers_since_cursor():
    port, d = _setup(n_orders=8, n_loads=0)
    nxt, items = d.containers_since(0)
    assert len(items) == 8 and nxt == items[-1].seq
    nxt2, again = d.containers_since(nxt)
    assert again == [] and nxt2 == nxt  # nothing new: the cursor stays
    _finish(port, d, 3)
    nxt3, newer = d.containers_since(nxt)
    assert [v.state for v in newer] == ["done"] * 3 and nxt3 > nxt
    assert [v.container_id for v in newer] == d.completed
    assert d.containers_since(nxt3) == (nxt3, [])
    # limit: pages in seq order, next = last returned seq
    p1_next, p1 = d.containers_since(0, limit=5)
    p2_next, p2 = d.containers_since(p1_next, limit=5)
    _, full = d.containers_since(0)
    assert len(p1) == 5 and p1 + p2 == full and p2_next == full[-1].seq


def test_containers_since_before_prune():
    """Review Focus 2: a cursor older than the retained window still gets the newest
    items and a usable ``next``."""
    port, d = _setup(n_orders=20, n_loads=0)
    _finish(port, d, 15)
    old_cursor = 3  # points into records that will be pruned
    assert d.prune_done(keep=5) == 10
    nxt, items = d.containers_since(old_cursor)
    assert [v.state for v in items].count("done") == 5 and [v.state for v in items].count("open") == 5
    assert nxt == max(v.seq for v in items)
    assert d.containers_since(nxt) == (nxt, [])
    # a cursor from beyond this dispatcher's history (e.g. another ECS instance): empty,
    # and ``next`` falls back to the newest seq so the client can resynchronise
    assert d.containers_since(10_000) == (nxt, [])
    with pytest.raises(ValueError):
        d.containers_since(-1)


def test_runner_paths_never_prune():
    """Without ``prune_done`` everything is kept (the runner and the equivalence fixtures)."""
    port, d = _setup(n_orders=12, n_loads=0)
    _finish(port, d, 12)
    assert len(d.completed) == 12 and d.status()["done"] == 12
    assert len(d.containers_since(0)[1]) == 12
