from __future__ import annotations

from collections import deque

from terminal_model.types import Position

Cell = tuple[int, int]  # (col x, row y), 0-based

__all__ = ["Cell", "Grid"]


class Grid:
    """Grid with one-way horizontal lanes and per-cell reservations.

    Even rows run towards +x, odd rows towards -x; vertical moves are always allowed.
    """

    def __init__(self, cols: int, rows: int, cell_m: float) -> None:
        self.cols = cols
        self.rows = rows
        self.cell_m = cell_m
        self._owners: dict[Cell, str] = {}
        self._waiting: dict[str, Cell] = {}  # owner -> cell it is blocked on

    def _check(self, c: Cell) -> None:
        x, y = c
        if not (0 <= x < self.cols and 0 <= y < self.rows):
            raise ValueError(f"cell {c} outside {self.cols}x{self.rows} grid")

    def neighbors(self, c: Cell) -> list[Cell]:
        self._check(c)
        x, y = c
        candidates = [(x + 1 if y % 2 == 0 else x - 1, y), (x, y - 1), (x, y + 1)]
        return [n for n in candidates if 0 <= n[0] < self.cols and 0 <= n[1] < self.rows]

    def shortest_path(
        self, start: Cell, goal: Cell, *, avoid: frozenset[Cell] | set[Cell] = frozenset()
    ) -> list[Cell]:
        self._check(start)
        self._check(goal)
        if start == goal:
            return [start]
        prev: dict[Cell, Cell] = {}
        seen = {start}
        queue = deque([start])
        while queue:
            cur = queue.popleft()
            for n in self.neighbors(cur):
                if n in seen or (n in avoid and n != goal):
                    continue
                seen.add(n)
                prev[n] = cur
                if n == goal:
                    path = [goal]
                    while path[-1] != start:
                        path.append(prev[path[-1]])
                    path.reverse()
                    return path
                queue.append(n)
        return []

    def try_reserve(self, c: Cell, owner: str) -> bool:
        self._check(c)
        current = self._owners.get(c)
        if current is not None and current != owner:
            return False
        self._owners[c] = owner
        return True

    def release(self, c: Cell, owner: str) -> None:
        self._check(c)
        if self._owners.get(c) == owner:
            del self._owners[c]

    def reserved_by_others(self, owner: str) -> set[Cell]:
        return {c for c, o in self._owners.items() if o != owner}

    def owner_of(self, c: Cell) -> str | None:
        return self._owners.get(c)

    def set_waiting(self, owner: str, c: Cell | None) -> None:
        if c is None:
            self._waiting.pop(owner, None)
        else:
            self._waiting[owner] = c

    def waiting_for(self, owner: str) -> Cell | None:
        return self._waiting.get(owner)

    def center(self, c: Cell) -> Position:
        self._check(c)
        return Position((c[0] + 0.5) * self.cell_m, (c[1] + 0.5) * self.cell_m)
