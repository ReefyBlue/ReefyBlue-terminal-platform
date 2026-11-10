from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_EQUIPMENT_RE = re.compile(r"^(STS|AGV)-[0-9]{2}$|^ASC-[A-Z]$")
_BLOCK_RE = re.compile(r"^[A-Z]$")
_INT_RE = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class EquipmentLoc:
    equipment_id: str


@dataclass(frozen=True)
class SlotLoc:
    block: str
    bay: int
    row: int
    tier: int


@dataclass(frozen=True)
class TransferPointLoc:
    block: str
    side: Literal["WS", "LS"]


Location = EquipmentLoc | SlotLoc | TransferPointLoc


def _check_block(block: str, s: object) -> None:
    if not _BLOCK_RE.fullmatch(block):
        raise ValueError(f"invalid block in location: {s!r}")


def parse_location(s: str) -> Location:
    if _EQUIPMENT_RE.fullmatch(s):
        return EquipmentLoc(s)
    parts = s.split("/")
    if len(parts) == 3 and parts[0] == "TP":
        _, block, side = parts
        _check_block(block, s)
        if side not in ("WS", "LS"):
            raise ValueError(f"invalid transfer point side: {s!r}")
        return TransferPointLoc(block, side)
    if len(parts) == 4:
        block, *nums = parts
        _check_block(block, s)
        if not all(_INT_RE.fullmatch(n) for n in nums):
            raise ValueError(f"invalid slot location: {s!r}")
        bay, row, tier = (int(n) for n in nums)
        if min(bay, row, tier) < 1:
            raise ValueError(f"bay/row/tier must be >= 1: {s!r}")
        return SlotLoc(block, bay, row, tier)
    raise ValueError(f"invalid location: {s!r}")


def format_location(loc: Location) -> str:
    match loc:
        case EquipmentLoc(equipment_id):
            if not _EQUIPMENT_RE.fullmatch(equipment_id):
                raise ValueError(f"invalid equipment id: {equipment_id!r}")
            return equipment_id
        case SlotLoc(block, bay, row, tier):
            _check_block(block, loc)
            if min(bay, row, tier) < 1:
                raise ValueError(f"bay/row/tier must be >= 1: {loc!r}")
            return f"{block}/{bay}/{row}/{tier}"
        case TransferPointLoc(block, side):
            _check_block(block, loc)
            if side not in ("WS", "LS"):
                raise ValueError(f"invalid transfer point side: {loc!r}")
            return f"TP/{block}/{side}"
    raise ValueError(f"unknown location: {loc!r}")
