from __future__ import annotations

from collections.abc import Mapping

from terminal_model.locations import SlotLoc, format_location


class SimpleSlotAllocator:
    """Hands out yard slots from its own bookkeeping (independent of the sim yard).

    ``blocks`` maps block name -> (bays, rows, tiers), in layout order.
    Implements ``terminal_ecs.workorders.SlotProvider``.
    """

    def __init__(self, blocks: Mapping[str, tuple[int, int, int]]) -> None:
        self._dims: dict[str, tuple[int, int, int]] = dict(blocks)  # layout order
        self._issued: dict[str, set[str]] = {b: set() for b in self._dims}

    def _first_free(self, block: str, exclude: str | None) -> str | None:
        # Same fill order as the sim Yard.find_slot: bays asc, rows asc, lowest tier.
        bays, rows, tiers = self._dims[block]
        issued = self._issued[block]
        for bay in range(1, bays + 1):
            for row in range(1, rows + 1):
                for tier in range(1, tiers + 1):
                    slot = format_location(SlotLoc(block, bay, row, tier))
                    if slot not in issued and slot != exclude:
                        return slot
        return None

    def next_slot(self, block: str, exclude: str | None = None) -> str | None:
        if block not in self._dims:
            raise KeyError(block)
        # Requested block first, then the others in layout (A-D) order.
        order = [block] + [b for b in self._dims if b != block]
        for candidate in order:
            slot = self._first_free(candidate, exclude)
            if slot is not None:
                self._issued[candidate].add(slot)
                return slot
        return None

    def release(self, slot: str) -> None:
        self._issued[slot.split("/", 1)[0]].discard(slot)
