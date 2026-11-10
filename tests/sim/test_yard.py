from pathlib import Path

import pytest

from terminal_model.locations import SlotLoc
from terminal_sim.layout import load_layout
from terminal_sim.yard import Yard


@pytest.fixture
def yard() -> Yard:
    return Yard(load_layout(Path("terminal.yaml")).blocks)


def test_find_slot_fills_tiers_first(yard: Yard):
    found = []
    for i in range(5):
        slot = yard.find_slot("A")
        found.append(slot)
        yard.occupy(slot, f"C{i}")
    assert found[:4] == [SlotLoc("A", 1, 1, t) for t in (1, 2, 3, 4)]
    assert found[4] == SlotLoc("A", 1, 2, 1)


def test_occupy_twice_raises(yard: Yard):
    slot = SlotLoc("A", 1, 1, 1)
    yard.occupy(slot, "C1")
    with pytest.raises(ValueError, match="SLOT_OCCUPIED"):
        yard.occupy(slot, "C2")
    assert yard.locate("C2") is None
    assert not yard.is_free(slot)


def test_block_full_returns_none(yard: Yard):
    assert yard.capacity("A") == 960
    for i in range(960):
        yard.occupy(yard.find_slot("A"), f"C{i}")
    assert yard.find_slot("A") is None
    assert yard.occupancy("A") == 1.0
    assert yard.find_slot("B") == SlotLoc("B", 1, 1, 1)
    assert yard.occupancy("B") == 0.0


def test_release_and_locate(yard: Yard):
    slot = SlotLoc("B", 2, 3, 1)
    yard.occupy(slot, "C1")
    assert yard.locate("C1") == slot
    assert yard.release(slot) == "C1"
    assert yard.locate("C1") is None
    assert yard.is_free(slot)
    with pytest.raises(KeyError):
        yard.release(slot)


def test_unknown_block_raises_keyerror(yard: Yard):
    with pytest.raises(KeyError):
        yard.find_slot("Z")
    with pytest.raises(KeyError):
        yard.capacity("Z")
    with pytest.raises(KeyError):
        yard.occupancy("Z")


@pytest.mark.parametrize(
    "slot",
    [
        SlotLoc("A", 99, 1, 1),
        SlotLoc("A", 0, 1, 1),
        SlotLoc("A", 1, 7, 1),
        SlotLoc("A", 1, 1, 5),
        SlotLoc("A", 1, 1, 0),
    ],
)
def test_out_of_range_slot_rejected(yard: Yard, slot: SlotLoc):
    with pytest.raises(ValueError, match="SLOT_OUT_OF_RANGE"):
        yard.occupy(slot, "X")
    with pytest.raises(ValueError, match="SLOT_OUT_OF_RANGE"):
        yard.is_free(slot)
    with pytest.raises(ValueError, match="SLOT_OUT_OF_RANGE"):
        yard.release(slot)
    assert yard.locate("X") is None
    assert yard.occupancy("A") == 0.0
    assert yard.find_slot("A") == SlotLoc("A", 1, 1, 1)


def test_occupy_already_placed_container_raises(yard: Yard):
    first, second = SlotLoc("A", 1, 1, 1), SlotLoc("A", 1, 1, 2)
    yard.occupy(first, "C1")
    with pytest.raises(ValueError, match="CONTAINER_PLACED"):
        yard.occupy(second, "C1")
    assert yard.locate("C1") == first
    assert yard.is_free(second)
    assert yard.release(first) == "C1"
    assert yard.occupancy("A") == 0.0


def test_is_free_unknown_block_raises_keyerror(yard: Yard):
    with pytest.raises(KeyError):
        yard.is_free(SlotLoc("Z", 1, 1, 1))
