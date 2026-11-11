"""The soak driver's yard bookkeeping (spec B2c §3.2).

The driver decides every slot itself: a DISCHARGE takes a free slot, a LOAD takes a
slot whose box an earlier DISCHARGE stored. Each slot is in one of these states:

- free (not tracked)
- ``IN``: a DISCHARGE order is on its way into it
- ``STORED``: a box is there and no order touches it -> loadable
- ``OUT``: a LOAD order is taking its box out
- ``UNKNOWN``: a LOAD failed; whether the box is still there is unknown, so the
  slot is neither free nor loadable (a rare, bounded leak instead of a DISCHARGE
  onto a box or a LOAD from an empty slot)

``occupied_count`` counts every tracked slot (not free); ``loadable_count`` the
``STORED`` ones. ``apply()`` folds one ``GET /containers`` item into this state.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from terminal_model.locations import SlotLoc, format_location


class SlotState(StrEnum):
    IN = "in"
    STORED = "stored"
    OUT = "out"
    UNKNOWN = "unknown"


class Yard:
    """``blocks`` maps block name -> (bays, rows, tiers), in layout order. Also a
    ``terminal_ecs.workorders.SlotProvider`` (``next_slot`` reserves a free slot)."""

    def __init__(self, blocks: Mapping[str, tuple[int, int, int]]) -> None:
        # Fill order per block as the sim Yard.find_slot: bays asc, rows asc, lowest tier.
        self._order: dict[str, list[str]] = {
            b: [format_location(SlotLoc(b, bay, row, tier))
                for bay in range(1, bays + 1) for row in range(1, rows + 1) for tier in range(1, tiers + 1)]
            for b, (bays, rows, tiers) in blocks.items()
        }
        self._slots: dict[str, tuple[SlotState, str]] = {}  # slot -> (state, container id)

    @classmethod
    def from_layout(cls, layout: Any) -> Yard:
        """From a ``terminal_sim.layout.Layout`` (or anything with ``blocks`` of
        ``block``/``bays``/``rows``/``tiers``)."""
        return cls({b.block: (int(b.bays), int(b.rows), int(b.tiers)) for b in layout.blocks})

    # -- queries -----------------------------------------------------------------------
    def blocks(self) -> list[str]:
        return list(self._order)

    @property
    def capacity(self) -> int:
        return sum(len(v) for v in self._order.values())

    @property
    def occupied_count(self) -> int:
        return len(self._slots)

    @property
    def loadable_count(self) -> int:
        return sum(st is SlotState.STORED for st, _ in self._slots.values())

    def state(self, slot: str) -> tuple[SlotState, str] | None:
        return self._slots.get(slot)

    def pick_free(self, block: str, exclude: str | None = None) -> str | None:
        """First free slot of ``block`` in fill order (not taken); ``KeyError`` for an
        unknown block."""
        for slot in self._order[block]:
            if slot not in self._slots and slot != exclude:
                return slot
        return None

    def pick_occupied(self, block: str) -> tuple[str, str] | None:
        """First ``STORED`` slot of ``block`` in fill order and its box id (not taken)."""
        for slot in self._order[block]:
            entry = self._slots.get(slot)
            if entry is not None and entry[0] is SlotState.STORED:
                return slot, entry[1]
        return None

    # -- updates -----------------------------------------------------------------------
    def _check(self, slot: str) -> None:
        if slot.split("/", 1)[0] not in self._order:
            raise KeyError(slot)

    def occupy(self, slot: str, cid: str) -> None:
        """A box ``cid`` is stored in ``slot``."""
        self._check(slot)
        self._slots[slot] = (SlotState.STORED, cid)

    def free(self, slot: str) -> None:
        """``slot`` is empty (idempotent)."""
        self._slots.pop(slot, None)

    def reserve_in(self, slot: str, cid: str) -> None:
        """DISCHARGE ``cid`` is on its way into ``slot``."""
        self._check(slot)
        self._slots[slot] = (SlotState.IN, cid)

    def reserve_out(self, slot: str, cid: str) -> None:
        """LOAD ``cid`` takes the box out of ``slot``."""
        self._check(slot)
        self._slots[slot] = (SlotState.OUT, cid)

    def next_slot(self, block: str, exclude: str | None = None) -> str | None:
        """``SlotProvider``: reserve (``IN``) the first free slot of ``block``, then of the
        other blocks in layout order; ``None`` when the yard is full."""
        if block not in self._order:
            raise KeyError(block)
        for candidate in [block] + [b for b in self._order if b != block]:
            slot = self.pick_free(candidate, exclude)
            if slot is not None:
                self._slots[slot] = (SlotState.IN, "")
                return slot
        return None

    def apply(self, cid: str, state: str, job_type: str, slot: str) -> None:
        """Fold one ``GET /containers`` item in (items come in ``seq`` order, each order
        once with its latest state). Items of unknown blocks are ignored. A finished
        order never touches a slot that a different *open* order (``IN``/``OUT``) holds:
        that order is newer."""
        if slot.split("/", 1)[0] not in self._order:
            return
        entry = self._slots.get(slot)
        held_by_other = entry is not None and entry[1] != cid and entry[0] in (SlotState.IN, SlotState.OUT)
        if state == "open":
            self._slots[slot] = (SlotState.IN if job_type == "DISCHARGE" else SlotState.OUT, cid)
        elif held_by_other:
            return
        elif job_type == "DISCHARGE":
            if state == "done":
                self._slots[slot] = (SlotState.STORED, cid)
            elif state == "failed" and entry is not None and entry[1] == cid:
                del self._slots[slot]  # the box never reached the stack
        elif job_type == "LOAD":
            # On a resume a finished LOAD may be the first item seen for its slot (its
            # open state was overwritten by the finish), so it acts on a STORED box too.
            if state == "done":
                self._slots.pop(slot, None)
            elif state == "failed":
                self._slots[slot] = (SlotState.UNKNOWN, cid)
