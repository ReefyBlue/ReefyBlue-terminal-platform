import pytest

from terminal_model.locations import (
    EquipmentLoc,
    SlotLoc,
    TransferPointLoc,
    format_location,
    parse_location,
)


@pytest.mark.parametrize(
    "s,loc",
    [
        ("STS-01", EquipmentLoc("STS-01")),
        ("AGV-03", EquipmentLoc("AGV-03")),
        ("ASC-A", EquipmentLoc("ASC-A")),
        ("A/12/3/2", SlotLoc("A", 12, 3, 2)),
        ("TP/B/WS", TransferPointLoc("B", "WS")),
        ("TP/A/LS", TransferPointLoc("A", "LS")),
    ],
)
def test_roundtrip(s, loc):
    assert parse_location(s) == loc
    assert format_location(loc) == s


@pytest.mark.parametrize(
    "s",
    ["", "A/12/3", "TP/A/XX", "A/0/1/1", "A/1/0/1", "A/1/1/0", "STS-1", "ASC-AB",
     "XYZ-01", "STS-01\n", "A/١/1/1", "a/1/1/1", "AB/1/1/1", "A/x/1/1", "TP/AB/WS", "A/1/1/1/1"],
)
def test_parse_rejects(s):
    with pytest.raises(ValueError):
        parse_location(s)


def test_format_rejects_invalid():
    with pytest.raises(ValueError):
        format_location(SlotLoc("A", 0, 1, 1))
