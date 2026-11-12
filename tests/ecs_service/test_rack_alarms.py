"""C1b Task 5 review: the rack-mode WARNING alarm ``NO_FREE_COLUMN`` through the ECS service
(``GET /alarms`` schema, ``severity``, one alarm event per waiting order)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.workorders import WorkOrder
from terminal_ecs_service.app import create_app
from terminal_ecs_service.core import EcsCore
from terminal_ecs_service.events import Broadcaster
from terminal_model.locations import SlotLoc
from terminal_model.types import JobType
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_sim.world import make_world

ROOT = Path(__file__).resolve().parents[2]
MV_SMALL = ROOT / "terminal_mv_small.yaml"
MV_SCENARIO = ROOT / "tests" / "e2e" / "fixtures" / "mv_small_baseline.yaml"


class _NoSlots:
    def next_slot(self, block: str, exclude: str | None = None) -> str | None:
        return None


def _put(world, cid: str, bay: int, row: int) -> str:
    tier = world.stack.column_height("A", bay - 1, row - 1) + 1
    world.yard.occupy(SlotLoc("A", bay, row, tier), cid)
    return f"A/{bay}/{row}/{tier}"


def _drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_no_free_column_alarms_are_served_per_order_with_warning_severity():
    layout = load_layout(MV_SMALL)
    world = make_world(layout, load_scenario(MV_SCENARIO, layout))
    b = Broadcaster()
    q = b.register()
    core = EcsCore(layout, connect=lambda: world.port, broadcaster=b)
    assert await core.connect_once() is True
    d = core.dispatcher = Dispatcher(world.port, _NoSlots(), layout=layout.terminal, auto_release=True)
    # Two loads in block A under boxes; every other dry column of A (and L1's own) is full.
    slots = {"L0": _put(world, "L0", 20, 5), "L1": _put(world, "L1", 21, 5)}
    inv = {"X0": _put(world, "X0", 20, 5)}
    inv |= {f"X1-{i}": _put(world, f"X1-{i}", 21, 5) for i in range(4)}  # L1's column is full
    for bay in range(1, 41):
        for row in range(1, 10):
            while world.stack.column_height("A", bay - 1, row - 1) < 5 and (bay, row) not in ((20, 5), (21, 5)):
                cid = f"F{bay}-{row}-{world.stack.column_height('A', bay - 1, row - 1)}"
                inv[cid] = _put(world, cid, bay, row)
    d.register_stack(inv)
    d.submit([WorkOrder(c, JobType.LOAD, "STS-01", s) for c, s in slots.items()])
    _drain(q)
    core.publish_alarms()
    raised = [m for m in _drain(q) if m["type"] == "alarm" and m["action"] == "raised"]
    assert sorted((m["kind"], m["equipment_id"], m["container_id"], m["severity"]) for m in raised) == [
        ("NO_FREE_COLUMN", "ASC-A-LS", "L0", "WARNING"), ("NO_FREE_COLUMN", "ASC-A-LS", "L1", "WARNING")]

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(core, run_loop=False)),
                               base_url="http://t")
    async with client:
        r = await client.get("/alarms")
        assert r.status_code == 200
        body = r.json()
        assert [(a["kind"], a["severity"], a["container_id"], a["job_id"]) for a in body] == [
            ("NO_FREE_COLUMN", "WARNING", "L0", None), ("NO_FREE_COLUMN", "WARNING", "L1", None)]
        # an ERROR alarm next to them
        world.equipment["AGV-01"].set_state(world.equipment["AGV-01"].state.__class__.FAULT)
        body = (await client.get("/alarms")).json()
        assert ("NEEDS_RELEASE", "ERROR", None) in {(a["kind"], a["severity"], a["container_id"]) for a in body}

    # A truck takes one box away: L0 digs on into that column, which frees room in L0's own column
    # for L1. Each order's alarm is cleared on its own.
    d.submit([WorkOrder("F33-2-4", JobType.TRUCK_OUT, "", inv["F33-2-4"])])
    world.run(world.now() + 1000)
    _drain(q)
    core.publish_alarms()
    cleared = [m for m in _drain(q) if m["type"] == "alarm" and m["action"] == "cleared"]
    assert sorted((m["kind"], m["container_id"]) for m in cleared) == [
        ("NO_FREE_COLUMN", "L0"), ("NO_FREE_COLUMN", "L1")]
