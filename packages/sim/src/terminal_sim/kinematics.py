"""Kinematic axes and Konecranes-order-of-magnitude equipment defaults (pure, no salabim).

A move is a trapezoidal (or, when too short to reach ``v_max``, triangular) velocity profile.
Defaults can be overridden per equipment kind and per equipment id via the layout's
``kinematics:`` section (dotted ``axis.param`` keys, plus scalar parameters).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from terminal_model.layout import TerminalLayout

SAMPLE_DT = 0.5  # s of sim time between OPC UA position updates during a move


@dataclass(frozen=True)
class Axis:
    v_max: float
    accel: float
    decel: float

    def _profile(self, d: float) -> tuple[float, float, float]:
        """(peak speed, accel time, decel time) for a move of ``d`` m."""
        if d >= self.v_max**2 / (2 * self.accel) + self.v_max**2 / (2 * self.decel):
            return self.v_max, self.v_max / self.accel, self.v_max / self.decel
        vp = math.sqrt(2 * d * self.accel * self.decel / (self.accel + self.decel))
        return vp, vp / self.accel, vp / self.decel

    def travel_time(self, dist_m: float) -> float:
        d = abs(dist_m)
        if d <= 0:
            return 0.0
        vp, ta, td = self._profile(d)
        cruise = d - vp * vp / (2 * self.accel) - vp * vp / (2 * self.decel)
        return ta + td + max(cruise, 0.0) / vp

    def position_at(self, dist_m: float, t: float) -> float:
        """Distance covered after ``t`` s of a move of ``dist_m`` m, clamped to [0, dist_m]."""
        d = abs(dist_m)
        if d <= 0 or t <= 0:
            return 0.0
        vp, ta, td = self._profile(d)
        total = self.travel_time(d)
        if t >= total:
            return d
        if t < ta:
            x = 0.5 * self.accel * t * t
        else:
            d_acc = 0.5 * self.accel * ta * ta
            t_cruise = total - ta - td
            if t < ta + t_cruise:
                x = d_acc + vp * (t - ta)
            else:
                r = total - t  # time left; decelerating to rest
                x = d - 0.5 * self.decel * r * r
        return min(max(x, 0.0), d)


@dataclass(frozen=True)
class StsKin:
    trolley: Axis
    hoist_laden: Axis
    hoist_empty: Axis
    gantry: Axis
    twistlock_s: float


@dataclass(frozen=True)
class AgvKin:
    straight: Axis
    turning: Axis
    zone: Axis
    rack_position_s: float
    rack_lift_s: float
    length_m: float
    width_m: float


@dataclass(frozen=True)
class AscKin:
    gantry: Axis
    trolley: Axis
    hoist_laden: Axis
    hoist_empty: Axis
    spreader_lock_s: float


DEFAULTS: dict[str, StsKin | AgvKin | AscKin] = {
    "STS": StsKin(
        trolley=Axis(3.5, 0.8, 0.8),
        hoist_laden=Axis(1.4, 0.6, 0.6),
        hoist_empty=Axis(2.8, 0.6, 0.6),
        gantry=Axis(0.75, 0.2, 0.2),
        twistlock_s=12.0,
    ),
    "AGV": AgvKin(
        straight=Axis(6.0, 1.0, 1.2),
        turning=Axis(3.0, 1.0, 1.2),
        zone=Axis(1.0, 1.0, 1.2),
        rack_position_s=8.0, rack_lift_s=12.0, length_m=14.8, width_m=3.0,
    ),
    "ASC": AscKin(
        gantry=Axis(2.4, 0.5, 0.5),
        trolley=Axis(1.17, 0.5, 0.5),
        hoist_laden=Axis(0.75, 0.4, 0.4),
        hoist_empty=Axis(1.5, 0.4, 0.4),
        spreader_lock_s=6.0,
    ),
}


def _apply(kin, overrides: dict[str, float]):
    for key, val in overrides.items():
        if "." in key:
            axis_name, param = key.split(".", 1)
            kin = replace(kin, **{axis_name: replace(getattr(kin, axis_name), **{param: float(val)})})
        else:
            kin = replace(kin, **{key: float(val)})
    return kin


def kin_for(layout: TerminalLayout, equipment_id: str, kind: str):
    """DEFAULTS[kind], overridden by ``layout.kinematics[kind]`` then ``[equipment_id]``."""
    kin = DEFAULTS[kind]
    for key in (kind, equipment_id):
        kin = _apply(kin, layout.kinematics.get(key, {}))
    return kin


def sample_times(total_s: float, dt: float = SAMPLE_DT) -> list[float]:
    """Sample instants in (0, total_s] every ``dt`` s, always ending exactly at ``total_s``."""
    if total_s <= 0:
        return []
    n = int(total_s / dt + 1e-9)
    out = [round(i * dt, 9) for i in range(1, n + 1)]
    if not out or out[-1] < total_s - 1e-9:
        out.append(total_s)
    return out
