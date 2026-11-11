from pathlib import Path

import pytest
from terminal_model.layout import load_terminal
from terminal_sim.equipment.racks import RackError, Racks

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def racks():
    return Racks(load_terminal(ROOT / "terminal_mv_small.yaml").transfer_zones)


def test_free_rack_first_free_non_reefer(racks):
    r = racks.free_rack("A")
    assert r.id == "RK-A-1" and not r.reefer
    racks.put("RK-A-1", "c1")
    assert racks.free_rack("A").id == "RK-A-2"
    assert racks.occupant("RK-A-1") == "c1"


def test_reefer_rule(racks):
    assert racks.free_rack("A", reefer=True).id == "RK-A-R"
    racks.put("RK-A-R", "r1")
    assert racks.free_rack("A", reefer=True) is None
    for i in range(1, 6):
        racks.put(f"RK-A-{i}", f"c{i}")
    assert racks.free_rack("A") is None  # never falls back to the reefer rack


def test_take_and_errors(racks):
    racks.put("RK-A-1", "c1")
    with pytest.raises(RackError, match="RK-A-1"):
        racks.put("RK-A-1", "c2")
    assert racks.take("RK-A-1") == "c1"
    assert racks.occupant("RK-A-1") is None
    with pytest.raises(RackError, match="RK-A-1"):
        racks.take("RK-A-1")
    with pytest.raises(RackError, match="NOPE"):
        racks.put("NOPE", "c")
    with pytest.raises(RackError, match="Z"):
        racks.free_rack("Z")


def test_occupancy(racks):
    assert racks.occupancy("A") == (0, 6)
    racks.put("RK-A-2", "c")
    racks.put("RK-A-R", "r")
    assert racks.occupancy("A") == (2, 6)
