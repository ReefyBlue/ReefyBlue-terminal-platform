from __future__ import annotations

from terminal_model.locations import SlotLoc, format_location
from terminal_sim.layout import BlockSpec


class SimpleSlotAllocator:
    """Hands out yard slots from its own bookkeeping (independent of ``Yard``).

    Implements ``terminal_ecs.workorders.SlotProvider``.
    """

    def __init__(self, blocks: list[BlockSpec]) -> None:
        self._specs: dict[str, BlockSpec] = {b.block: b for b in blocks}  # layout order
        self._issued: dict[str, set[str]] = {b.block: set() for b in blocks}

    def _first_free(self, block: str, exclude: str | None) -> str | None:
        # Same fill order as Yard.find_slot: bays asc, rows asc, lowest tier.
        spec = self._specs[block]
        issued = self._issued[block]
        for bay in range(1, spec.bays + 1):
            for row in range(1, spec.rows + 1):
                for tier in range(1, spec.tiers + 1):
                    slot = format_location(SlotLoc(block, bay, row, tier))
                    if slot not in issued and slot != exclude:
                        return slot
        return None

    def next_slot(self, block: str, exclude: str | None = None) -> str | None:
        if block not in self._specs:
            raise KeyError(block)
        # Requested block first, then the others in layout (A-D) order.
        order = [block] + [b for b in self._specs if b != block]
        for candidate in order:
            slot = self._first_free(candidate, exclude)
            if slot is not None:
                self._issued[candidate].add(slot)
                return slot
        return None

    def release(self, slot: str) -> None:
        self._issued[slot.split("/", 1)[0]].discard(slot)
