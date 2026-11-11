"""Sampled holds: a move is held in ``SAMPLE_DT`` slices so the position can be updated
(OPC UA / viewer see the equipment move, not jump)."""
from __future__ import annotations

from collections.abc import Callable, Iterator

import salabim

from terminal_sim.kinematics import sample_times


def sampled_hold(
    comp: salabim.Component, total_s: float, update: Callable[[float], None]
) -> Iterator:
    """Hold ``total_s`` in slices of ``SAMPLE_DT``, calling ``update(t)`` after each slice
    with the time since the start (the last call is ``update(total_s)``)."""
    prev = 0.0
    for t in sample_times(total_s):
        yield comp.hold(t - prev)
        prev = t
        update(t)
