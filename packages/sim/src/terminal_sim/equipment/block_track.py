"""Shared crane track of an ASC pair in one block (pure, no salabim).

Positions are in bay units along the block (bay b centre = b; negative = waterside beyond the
stack, e.g. the transfer racks; > bays-1 = landside, e.g. the truck head). The WS crane is always
on the water side of the LS crane: ``ls_extent.lo - ws_extent.hi >= min_gap_bays``, where a
crane's extent is its claimed interval while it moves and its position otherwise.

- ``claim(asc, from, to, deadline_s)`` reserves the interval of a gantry move; False (and the
  wish is remembered) when it would come closer than the gap to the other crane's extent.
- ``release(asc, at)`` ends the move at ``at``.
- ``yield_request(from_asc, to_asc)`` asks the other crane to clear the last refused wish of
  ``from_asc``: it gets a target position (as far as needed, within the track limits) and its
  ``on_yield`` callback is called. Who honours it when is the cranes' business: an idle crane
  always, a waiting LS crane always, a waiting WS crane never (WS has priority).
"""
from __future__ import annotations

from collections.abc import Callable

WS, LS = "WS", "LS"


class BlockTrack:
    def __init__(self, bays: int, min_gap_bays: float, *, lo: float | None = None,
                 hi: float | None = None) -> None:
        self.bays = bays
        self.gap = float(min_gap_bays)
        # Parking room beyond the stack so either crane can reach every bay of the stack.
        self.lo = min(-self.gap, lo if lo is not None else -self.gap)
        self.hi = max(bays - 1 + self.gap, hi if hi is not None else bays - 1 + self.gap)
        self.role: dict[str, str] = {}
        self.pos: dict[str, float] = {}
        self.claims: dict[str, tuple[float, float]] = {}
        self.deadlines: dict[str, float] = {}
        self.wanted: dict[str, tuple[float, float]] = {}
        self.requests: dict[str, float] = {}
        self._on_yield: dict[str, Callable[[], None]] = {}

    def register(self, asc_id: str, role: str, pos: float,
                 on_yield: Callable[[], None] | None = None) -> None:
        if role not in (WS, LS) or role in self.role.values():
            raise ValueError(f"bad or duplicate role {role!r}")
        self.role[asc_id] = role
        self.pos[asc_id] = float(pos)
        if on_yield is not None:
            self._on_yield[asc_id] = on_yield
        if len(self.role) == 2 and self.separation() < self.gap - 1e-9:
            raise ValueError("cranes registered closer than the minimum gap")

    def other(self, asc_id: str) -> str | None:
        return next((a for a in self.role if a != asc_id), None)

    def extent(self, asc_id: str) -> tuple[float, float]:
        if asc_id in self.claims:
            return self.claims[asc_id]
        p = self.pos[asc_id]
        return (p, p)

    def separation(self) -> float:
        """Smallest distance LS - WS of the two extents (inf with fewer than two cranes)."""
        ws = next((a for a, r in self.role.items() if r == WS), None)
        ls = next((a for a, r in self.role.items() if r == LS), None)
        if ws is None or ls is None:
            return float("inf")
        return self.extent(ls)[0] - self.extent(ws)[1]

    def _ok(self, asc_id: str, iv: tuple[float, float]) -> bool:
        o = self.other(asc_id)
        if o is None:
            return True
        olo, ohi = self.extent(o)
        if self.role[asc_id] == WS:
            return iv[1] + self.gap <= olo + 1e-9
        return iv[0] - self.gap >= ohi - 1e-9

    def claim(self, asc_id: str, bay_from: float, bay_to: float, deadline_s: float) -> bool:
        if not self.lo - 1e-9 <= bay_to <= self.hi + 1e-9:
            raise ValueError(f"track position {bay_to} outside [{self.lo}, {self.hi}]")
        iv = (min(bay_from, bay_to), max(bay_from, bay_to))
        if not self._ok(asc_id, iv):
            self.wanted[asc_id] = iv
            return False
        self.wanted.pop(asc_id, None)
        self.claims[asc_id] = iv
        self.deadlines[asc_id] = deadline_s
        return True

    def release(self, asc_id: str, at: float | None = None) -> None:
        if at is not None:
            self.pos[asc_id] = float(at)
        self.claims.pop(asc_id, None)
        self.deadlines.pop(asc_id, None)

    def yield_request(self, from_asc: str, to_asc: str) -> float | None:
        """Target position for ``to_asc`` clearing ``from_asc``'s refused wish (None: no wish)."""
        need = self.wanted.get(from_asc)
        if need is None:
            return None
        cur = self.pos[to_asc]
        if self.role[to_asc] == LS:
            target = min(max(cur, need[1] + self.gap), self.hi)
        else:
            target = max(min(cur, need[0] - self.gap), self.lo)
        if target == cur:
            return None  # nothing to clear by moving (the crane is in a move or already clear)
        self.requests[to_asc] = target
        cb = self._on_yield.get(to_asc)
        if cb is not None:
            cb()
        return target

    def pending_yield(self, asc_id: str) -> float | None:
        return self.requests.get(asc_id)

    def clear_yield(self, asc_id: str) -> None:
        self.requests.pop(asc_id, None)
