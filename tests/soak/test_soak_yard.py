"""The soak driver's own yard bookkeeping (Plan B2c Task 5)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from terminal_soak.yard import Yard

BLOCKS = {"A": (2, 2, 2), "B": (1, 1, 2)}


def test_yard_balance():
    y = Yard(BLOCKS)
    assert y.occupied_count == 0 and y.loadable_count == 0
    assert y.pick_free("A") == "A/1/1/1"  # same fill order as the sim: bay, row, lowest tier
    assert y.pick_occupied("A") is None
    y.occupy("A/1/1/1", "C1")
    y.occupy("A/1/2/1", "C2")
    assert y.occupied_count == 2 and y.loadable_count == 2
    assert y.pick_free("A") == "A/1/1/2"
    assert y.pick_free("A") == "A/1/1/2"  # picking does not take the slot
    assert y.pick_occupied("A") == ("A/1/1/1", "C1")
    assert y.pick_occupied("B") is None
    y.free("A/1/1/1")
    y.free("A/1/1/1")  # idempotent
    assert y.occupied_count == 1
    assert y.pick_free("A") == "A/1/1/1"
    assert y.pick_occupied("A") == ("A/1/2/1", "C2")
    with pytest.raises(KeyError):
        y.pick_free("Z")


def test_reservations_count_as_occupied_but_are_not_loadable():
    y = Yard(BLOCKS)
    y.reserve_in("A/1/1/1", "D1")  # a DISCHARGE on its way
    y.occupy("A/1/1/2", "C1")
    y.reserve_out("A/1/1/2", "L1")  # a LOAD on its way out
    assert y.occupied_count == 2 and y.loadable_count == 0
    assert y.pick_occupied("A") is None
    assert y.pick_free("A") == "A/1/2/1"


def test_full_block_and_next_slot_fallback():
    y = Yard(BLOCKS)
    for _ in range(8):
        slot = y.next_slot("A")
        assert slot is not None and slot.startswith("A/")
    assert y.pick_free("A") is None
    assert y.next_slot("A") == "B/1/1/1"  # then the other blocks in layout order
    assert y.next_slot("A", exclude="B/1/1/2") is None
    assert y.occupied_count == 9 and y.loadable_count == 0  # reserved, not yet stored


def test_apply_container_items():
    y = Yard(BLOCKS)
    y.apply("D1", "open", "DISCHARGE", "A/1/1/1")
    assert y.occupied_count == 1 and y.loadable_count == 0
    y.apply("D1", "done", "DISCHARGE", "A/1/1/1")
    assert y.pick_occupied("A") == ("A/1/1/1", "D1")
    y.apply("L1", "open", "LOAD", "A/1/1/1")
    assert y.occupied_count == 1 and y.loadable_count == 0
    y.apply("L1", "done", "LOAD", "A/1/1/1")
    assert y.occupied_count == 0
    # a failed DISCHARGE never placed its box
    y.apply("D2", "open", "DISCHARGE", "A/1/1/2")
    y.apply("D2", "failed", "DISCHARGE", "A/1/1/2")
    assert y.occupied_count == 0
    # a failed LOAD leaves an unknown slot: kept out of both pools
    y.apply("D3", "done", "DISCHARGE", "A/1/2/1")
    y.apply("L3", "open", "LOAD", "A/1/2/1")
    y.apply("L3", "failed", "LOAD", "A/1/2/1")
    assert y.occupied_count == 1 and y.loadable_count == 0 and y.pick_free("A") == "A/1/1/1"
    # a finished order no longer owning its slot does not touch it
    y.apply("D4", "open", "DISCHARGE", "A/1/1/1")
    y.apply("D9", "failed", "DISCHARGE", "A/1/1/1")
    assert y.occupied_count == 2
    # resume: a finished LOAD is the first item seen for a slot a DISCHARGE stored
    y.apply("D5", "done", "DISCHARGE", "A/2/1/1")
    y.apply("L5", "done", "LOAD", "A/2/1/1")
    assert y.state("A/2/1/1") is None
    y.apply("D6", "done", "DISCHARGE", "A/2/1/2")
    y.apply("L6", "failed", "LOAD", "A/2/1/2")
    assert y.state("A/2/1/2") == ("unknown", "L6")
    # slots of blocks the driver does not know are ignored
    y.apply("X1", "done", "DISCHARGE", "Z/1/1/1")
    assert y.occupied_count == 3


def test_from_layout():
    layout = SimpleNamespace(blocks=[SimpleNamespace(block="A", bays=3, rows=2, tiers=1)])
    y = Yard.from_layout(layout)
    assert y.blocks() == ["A"] and y.capacity == 6
