from __future__ import annotations

from terminal_model.locations import SlotLoc
from terminal_sim.layout import BlockSpec


class Yard:
    """In-memory slot occupancy for the stack blocks."""

    def __init__(self, blocks: list[BlockSpec]) -> None:
        self._specs: dict[str, BlockSpec] = {b.block: b for b in blocks}
        self._by_slot: dict[SlotLoc, str] = {}
        self._by_container: dict[str, SlotLoc] = {}
        self._count: dict[str, int] = {b.block: 0 for b in blocks}

    def find_slot(self, block: str) -> SlotLoc | None:
        """First free slot: bays ascending, rows ascending, lowest free tier."""
        spec = self._specs[block]
        if self._count[block] >= self.capacity(block):
            return None
        for bay in range(1, spec.bays + 1):
            for row in range(1, spec.rows + 1):
                for tier in range(1, spec.tiers + 1):
                    slot = SlotLoc(block, bay, row, tier)
                    if slot not in self._by_slot:
                        return slot
        return None

    def is_free(self, slot: SlotLoc) -> bool:
        return slot not in self._by_slot

    def occupy(self, slot: SlotLoc, container_id: str) -> None:
        if slot.block not in self._specs:
            raise KeyError(slot.block)
        if slot in self._by_slot:
            raise ValueError("SLOT_OCCUPIED")
        self._by_slot[slot] = container_id
        self._by_container[container_id] = slot
        self._count[slot.block] += 1

    def release(self, slot: SlotLoc) -> str:
        container_id = self._by_slot.pop(slot)  # KeyError if empty
        del self._by_container[container_id]
        self._count[slot.block] -= 1
        return container_id

    def locate(self, container_id: str) -> SlotLoc | None:
        return self._by_container.get(container_id)

    def occupancy(self, block: str) -> float:
        return self._count[block] / self.capacity(block)

    def capacity(self, block: str) -> int:
        spec = self._specs[block]
        return spec.bays * spec.rows * spec.tiers
