"""Vessel model at a berth and the STS move geometry (pure, no salabim).

Frame: x along the quay, y landward (quay edge y = 0, the vessel lies at y < 0); heights in m
above the quay. Vessel row 0 is the row nearest the quay. Tiers 0..tiers_hold-1 are in the hold
(0 = bottom), the next ``tiers_deck`` tiers on deck (on the hatch covers).

One STS move is a list of single-axis segments; the spreader starts and ends over the lane
at the travel height of the move (above the target stack and the hatch, plus clearance).
"""
from __future__ import annotations

from dataclasses import dataclass

from terminal_model.layout import Berth

from terminal_sim.kinematics import Axis, StsKin

BAY_PITCH_M = 6.5
FIRST_BAY_OFFSET_M = 25.0
ROW_PITCH_M = 2.5
QUAY_TO_ROW0_M = 3.0  # quay edge to the quay-side edge of row 0
BOX_H_M = 2.6
DECK_H_M = 3.0  # hatch covers above the quay
CLEARANCE_M = 3.0
LANE_H_M = 2.5  # Lift-AGV platform / lane buffer rack top


@dataclass(frozen=True)
class Segment:
    """One axis moving: ``axis`` over ``trolley_from -> trolley_to`` or ``hoist_from -> hoist_to``."""

    name: str
    axis: Axis
    trolley_from: float
    trolley_to: float
    hoist_from: float
    hoist_to: float

    @property
    def dist(self) -> float:
        return abs(self.trolley_to - self.trolley_from) + abs(self.hoist_to - self.hoist_from)

    @property
    def duration(self) -> float:
        return self.axis.travel_time(self.dist)

    def at(self, t: float) -> tuple[float, float]:
        """(trolley y, hoist h) ``t`` s into the segment."""
        f = self.axis.position_at(self.dist, t) / self.dist if self.dist > 0 else 1.0
        return (self.trolley_from + f * (self.trolley_to - self.trolley_from),
                self.hoist_from + f * (self.hoist_to - self.hoist_from))


class Vessel:
    def __init__(self, berth: Berth, bays: int = 50, rows: int = 22, tiers_deck: int = 8,
                 tiers_hold: int = 10) -> None:
        self.berth = berth
        self.bays, self.rows = bays, rows
        self.tiers_deck, self.tiers_hold = tiers_deck, tiers_hold
        self.loaded: list[str] = []  # boxes put on board by LOAD moves (no stowage model)

    @property
    def tiers(self) -> int:
        return self.tiers_hold + self.tiers_deck

    def check(self, bay: int, row: int, tier: int) -> None:
        if not (0 <= bay < self.bays and 0 <= row < self.rows and 0 <= tier < self.tiers):
            raise ValueError(
                f"VESSEL_OUT_OF_RANGE: ({bay},{row},{tier}) not in {self.bays}x{self.rows}x{self.tiers}")

    def bay_x(self, i: int) -> float:
        return self.berth.from_m + FIRST_BAY_OFFSET_M + i * BAY_PITCH_M

    def row_y(self, row: int) -> float:
        return -(QUAY_TO_ROW0_M + (row + 0.5) * ROW_PITCH_M)

    def tier_h(self, tier: int) -> float:
        if tier < self.tiers_hold:
            return DECK_H_M - (self.tiers_hold - tier) * BOX_H_M
        return DECK_H_M + (tier - self.tiers_hold) * BOX_H_M

    def travel_h(self, tier: int) -> float:
        return max(self.tier_h(tier), DECK_H_M, LANE_H_M) + BOX_H_M + CLEARANCE_M

    def move_segments(self, kin: StsKin, row: int, tier: int, laden: bool, *,
                      lane_y_m: float) -> list[Segment]:
        """The segments of one move. ``laden=False``: DISCHARGE (out empty, back laden, box set
        down in the lane); ``laden=True``: LOAD (box picked in the lane, out laden, back empty)."""
        ry, h, top = self.row_y(row), self.tier_h(tier), self.travel_h(tier)
        e, L, tr = kin.hoist_empty, kin.hoist_laden, kin.trolley

        def tro(name, axis, a, b, hh):
            return Segment(name, axis, a, b, hh, hh)

        def hoi(name, axis, y, a, b):
            return Segment(name, axis, y, y, a, b)

        if not laden:
            return [
                tro("trolley_out", tr, lane_y_m, ry, top),
                hoi("hoist_down", e, ry, top, h),
                hoi("hoist_up", L, ry, h, top),
                tro("trolley_back", tr, ry, lane_y_m, top),
                hoi("lane_down", L, lane_y_m, top, LANE_H_M),
                hoi("lane_up", e, lane_y_m, LANE_H_M, top),
            ]
        return [
            hoi("lane_down", e, lane_y_m, top, LANE_H_M),
            hoi("lane_up", L, lane_y_m, LANE_H_M, top),
            tro("trolley_out", tr, lane_y_m, ry, top),
            hoi("hoist_down", L, ry, top, h),
            hoi("hoist_up", e, ry, h, top),
            tro("trolley_back", tr, ry, lane_y_m, top),
        ]

    def move_time(self, kin: StsKin, bay: int, row: int, tier: int, laden: bool, *,
                  lane_y_m: float = 53.0) -> float:
        """Trolley lane -> row, hoist down/up, back, set down/pick in the lane (no gantry, no
        twistlock handling)."""
        self.check(bay, row, tier)
        return sum(s.duration for s in self.move_segments(kin, row, tier, laden, lane_y_m=lane_y_m))
