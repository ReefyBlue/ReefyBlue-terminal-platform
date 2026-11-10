from __future__ import annotations

import random

import salabim
from terminal_model.types import State

from terminal_sim.equipment.base import Equipment
from terminal_sim.scenario import FaultParams


class FaultInjector(salabim.Component):
    """Alternates a target between working and FAULT (exponential MTBF/MTTR)."""

    def __init__(
        self,
        *,
        target: Equipment,
        params: FaultParams,
        rng: random.Random,
        env: salabim.Environment,
    ) -> None:
        self.target = target
        self.params = params
        self.rng = rng
        self.count = 0
        super().__init__(name=f"fault-{target.equipment_id}", env=env)

    def process(self):
        while True:
            yield self.hold(self.rng.expovariate(1 / self.params.mtbf_s))
            if self.target.state is State.FAULT:
                continue
            if self.target.current_job is not None:
                self.target.fail_job("FAULT")
            self.target.set_state(State.FAULT)
            self.count += 1
            yield self.hold(self.rng.expovariate(1 / self.params.mttr_s))
            self.target.set_state(State.IDLE)
