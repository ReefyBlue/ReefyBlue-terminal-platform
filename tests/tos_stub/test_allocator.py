from pathlib import Path

import pytest

from terminal_model.locations import format_location
from terminal_sim.layout import load_layout
from terminal_sim.yard import Yard
from terminal_tos_stub.allocator import SimpleSlotAllocator


@pytest.fixture
def layout():
    return load_layout(Path("terminal.yaml"))


def make(layout) -> SimpleSlotAllocator:
    return SimpleSlotAllocator({b.block: (b.bays, b.rows, b.tiers) for b in layout.blocks})


def test_fill_order_matches_yard(layout):
    alloc = make(layout)
    yard = Yard(layout.blocks)
    for i in range(50):
        expected = yard.find_slot("A")
        assert alloc.next_slot("A") == format_location(expected)
        yard.occupy(expected, f"X{i}")


def test_allocator_spills_to_next_block(layout):
    alloc = make(layout)
    a = layout.blocks[0]
    cap = a.bays * a.rows * a.tiers
    assert cap == 960
    slots = [alloc.next_slot(a.block) for _ in range(cap)]
    assert len(set(slots)) == cap
    assert all(s.startswith("A/") for s in slots)
    assert alloc.next_slot("A").startswith("B/")


def test_returns_none_when_everything_full(layout):
    alloc = make(layout)
    total = sum(b.bays * b.rows * b.tiers for b in layout.blocks)
    for _ in range(total):
        assert alloc.next_slot("A") is not None
    assert alloc.next_slot("A") is None


def test_exclude_skips_slot(layout):
    alloc = make(layout)
    first = alloc.next_slot("A")
    alloc.release(first)
    assert alloc.next_slot("A", exclude=first) != first
    # the excluded slot was not issued, so it is still available
    assert alloc.next_slot("A") == first


def test_release_reissues(layout):
    alloc = make(layout)
    first = alloc.next_slot("A")
    second = alloc.next_slot("A")
    assert second != first
    alloc.release(first)
    assert alloc.next_slot("A") == first


def test_unknown_block_raises(layout):
    alloc = make(layout)
    with pytest.raises(KeyError):
        alloc.next_slot("Z")


def test_plain_block_dimensions():
    alloc = SimpleSlotAllocator({"X": (1, 1, 2), "Y": (1, 1, 1)})
    assert [alloc.next_slot("X") for _ in range(4)] == ["X/1/1/1", "X/1/1/2", "Y/1/1/1", None]


def test_occupy_marks_slot_used():
    a = SimpleSlotAllocator({"A": (2, 1, 1)})
    a.occupy("A/1/1/1")
    a.occupy("A/1/1/1")
    assert a.next_slot("A") == "A/2/1/1"


def test_occupy_unknown_block_raises():
    a = SimpleSlotAllocator({"A": (2, 1, 1)})
    with pytest.raises(KeyError):
        a.occupy("Z/1/1/1")
