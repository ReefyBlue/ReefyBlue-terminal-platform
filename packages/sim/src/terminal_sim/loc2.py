"""Job location grammar of the v2 world (``World2``).

- ``VESSEL:<berth>:<bay>:<row>:<tier>`` - a vessel cell (bay 0..bays-1, row 0..21 from the quay,
  tier 0..tiers_hold-1 in the hold from the bottom, then the deck tiers).
- ``LANE:<lane_id>`` - an STS transfer lane (``LANE:*`` on an STS job: any of its lanes).
- ``RACK:<rack_id>``, ``BUFFER:<buffer_id>`` - transfer rack / apron buffer.
- ``SLOT:<block>:<bay>:<row>[:<tier>]`` - a stack column, 0-based like ``Stack``; the tier is
  optional and, when given, checked against the stack.
- ``TRUCK:<block>:<lane>`` - a truck lane (0-based) at the landside head of a block.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VesselLoc:
    berth: str
    bay: int
    row: int
    tier: int


@dataclass(frozen=True)
class LaneLoc:
    lane_id: str  # "*" = any lane of the STS


@dataclass(frozen=True)
class RackLoc:
    rack_id: str


@dataclass(frozen=True)
class BufferLoc:
    buffer_id: str


@dataclass(frozen=True)
class SlotLoc2:
    block: str
    bay: int
    row: int
    tier: int | None = None


@dataclass(frozen=True)
class TruckLoc:
    block: str
    lane: int


Loc2 = VesselLoc | LaneLoc | RackLoc | BufferLoc | SlotLoc2 | TruckLoc


def _ints(parts: list[str], s: str) -> list[int]:
    try:
        vals = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"invalid location {s!r}") from None
    if any(v < 0 for v in vals):
        raise ValueError(f"negative index in location {s!r}")
    return vals


def parse_loc2(s: str) -> Loc2:
    head, _, rest = s.partition(":")
    parts = rest.split(":") if rest else []
    if head == "VESSEL" and len(parts) == 4:
        return VesselLoc(parts[0], *_ints(parts[1:], s))
    if head == "LANE" and len(parts) == 1 and parts[0]:
        return LaneLoc(parts[0])
    if head == "RACK" and len(parts) == 1 and parts[0]:
        return RackLoc(parts[0])
    if head == "BUFFER" and len(parts) == 1 and parts[0]:
        return BufferLoc(parts[0])
    if head == "SLOT" and len(parts) in (3, 4):
        return SlotLoc2(parts[0], *_ints(parts[1:], s))
    if head == "TRUCK" and len(parts) == 2:
        return TruckLoc(parts[0], *_ints(parts[1:], s))
    raise ValueError(f"invalid location {s!r}")


def try_parse(s: str) -> Loc2 | None:
    try:
        return parse_loc2(s)
    except ValueError:
        return None
