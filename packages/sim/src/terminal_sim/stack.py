"""Container stack over v2 blocks: columns keyed (bay, row), tier 0 = ground (pure, no salabim).

Independent of the legacy slot-per-container ``terminal_sim.yard.Yard``.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from terminal_model.layout import Block

SlotLoc = tuple[str, int, int, int]  # (block, bay, row, tier)


class StackError(ValueError):
    """Illegal stack operation (full column, not on top, unknown block/container...)."""


class Stack:
    def __init__(self, blocks: Sequence[Block]) -> None:
        self._blocks: dict[str, Block] = {b.id: b for b in blocks}
        self._cols: dict[tuple[str, int, int], list[str]] = {}
        self._loc: dict[str, SlotLoc] = {}

    def _block(self, block: str) -> Block:
        try:
            return self._blocks[block]
        except KeyError:
            raise StackError(f"unknown block {block!r}") from None

    def _check(self, block: str, bay: int, row: int) -> Block:
        b = self._block(block)
        if not (0 <= bay < b.bays and 0 <= row < b.rows):
            raise StackError(f"column ({bay},{row}) outside block {block} ({b.bays}x{b.rows})")
        return b

    def put(self, block: str, bay: int, row: int, cid: str) -> SlotLoc:
        b = self._check(block, bay, row)
        if cid in self._loc:
            raise StackError(f"container {cid} already in stack at {self._loc[cid]}")
        col = self._cols.setdefault((block, bay, row), [])
        if len(col) >= b.tiers:
            raise StackError(f"column {block}/{bay}/{row} is full ({b.tiers} tiers)")
        col.append(cid)
        loc = (block, bay, row, len(col) - 1)
        self._loc[cid] = loc
        return loc

    def take(self, cid: str) -> SlotLoc:
        loc = self._loc.get(cid)
        if loc is None:
            raise StackError(f"container {cid} is not in the stack")
        col = self._cols[loc[:3]]
        if col[-1] != cid:
            raise StackError(f"container {cid} is not on top of {loc[:3]} (above: {self.above(cid)})")
        col.pop()
        del self._loc[cid]
        return loc

    def top(self, block: str, bay: int, row: int) -> str | None:
        col = self._cols.get((block, bay, row))
        return col[-1] if col else None

    def locate(self, cid: str) -> SlotLoc | None:
        return self._loc.get(cid)

    def above(self, cid: str) -> list[str]:
        loc = self._loc.get(cid)
        if loc is None:
            raise StackError(f"container {cid} is not in the stack")
        return list(reversed(self._cols[loc[:3]][loc[3] + 1:]))

    def column_height(self, block: str, bay: int, row: int) -> int:
        return len(self._cols.get((block, bay, row), ()))

    def free_columns(self, block: str, *, exclude_rows: Iterable[int] = ()) -> list[tuple[int, int]]:
        """Non-full columns as (bay, row), sorted by (height, bay, row)."""
        b = self._block(block)
        skip = set(exclude_rows)
        out = []
        for bay in range(b.bays):
            for row in range(b.rows):
                if row in skip:
                    continue
                h = self.column_height(block, bay, row)
                if h < b.tiers:
                    out.append((h, bay, row))
        out.sort()
        return [(bay, row) for _, bay, row in out]

    def free_columns_for(self, block: str, *, reefer: bool) -> list[tuple[int, int]]:
        """Reefer boxes only on reefer rows; other boxes never on reefer rows."""
        b = self._block(block)
        if not reefer:
            return self.free_columns(block, exclude_rows=b.reefer_rows)
        non = [r for r in range(b.rows) if r not in b.reefer_rows]
        return self.free_columns(block, exclude_rows=non)

    def occupancy(self, block: str) -> float:
        b = self._block(block)
        n = sum(len(c) for (bk, _, _), c in self._cols.items() if bk == block)
        return n / (b.bays * b.rows * b.tiers)
