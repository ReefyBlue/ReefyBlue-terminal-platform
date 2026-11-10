from pathlib import Path

import pytest

from terminal_model.locations import format_location, parse_location
from terminal_model.types import JobType
from terminal_ecs.workorders import WorkOrder
from terminal_sim.layout import load_layout
from terminal_sim.yard import Yard
from terminal_tos_stub.allocator import SimpleSlotAllocator
from terminal_tos_stub.generator import prefill


@pytest.fixture
def layout():
    return load_layout(Path("terminal.yaml"))


def test_fill_order_matches_yard(layout):
    alloc = SimpleSlotAllocator(layout.blocks)
    yard = Yard(layout.blocks)
    for i in range(50):
        expected = yard.find_slot("A")
        assert alloc.next_slot("A") == format_location(expected)
        yard.occupy(expected, f"X{i}")


def test_allocator_spills_to_next_block(layout):
    alloc = SimpleSlotAllocator(layout.blocks)
    a = layout.blocks[0]
    cap = a.bays * a.rows * a.tiers
    assert cap == 960
    slots = [alloc.next_slot(a.block) for _ in range(cap)]
    assert len(set(slots)) == cap
    assert all(s.startswith("A/") for s in slots)
    assert alloc.next_slot("A").startswith("B/")


def test_returns_none_when_everything_full(layout):
    alloc = SimpleSlotAllocator(layout.blocks)
    total = sum(b.bays * b.rows * b.tiers for b in layout.blocks)
    for _ in range(total):
        assert alloc.next_slot("A") is not None
    assert alloc.next_slot("A") is None


def test_exclude_skips_slot(layout):
    alloc = SimpleSlotAllocator(layout.blocks)
    first = alloc.next_slot("A")
    alloc.release(first)
    assert alloc.next_slot("A", exclude=first) != first
    # the excluded slot was not issued, so it is still available
    assert alloc.next_slot("A") == first


def test_release_reissues(layout):
    alloc = SimpleSlotAllocator(layout.blocks)
    first = alloc.next_slot("A")
    second = alloc.next_slot("A")
    assert second != first
    alloc.release(first)
    assert alloc.next_slot("A") == first


def test_unknown_block_raises(layout):
    alloc = SimpleSlotAllocator(layout.blocks)
    with pytest.raises(KeyError):
        alloc.next_slot("Z")


def test_prefill_puts_load_containers_in_yard(layout):
    yard = Yard(layout.blocks)
    orders = [
        WorkOrder("C0001", JobType.DISCHARGE, "STS-01", "A/1/1/1"),
        WorkOrder("C0002", JobType.LOAD, "STS-01", "A/1/1/2"),
        WorkOrder("C0003", JobType.LOAD, "STS-02", "B/2/3/1"),
    ]
    prefill(yard, orders)
    assert yard.locate("C0001") is None
    assert yard.locate("C0002") == parse_location("A/1/1/2")
    assert yard.locate("C0003") == parse_location("B/2/3/1")
