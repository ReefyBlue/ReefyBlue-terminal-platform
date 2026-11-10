from __future__ import annotations

import random

from terminal_ecs.workorders import WorkOrder
from terminal_model.locations import parse_location
from terminal_model.types import JobType
from terminal_sim.layout import Layout
from terminal_sim.scenario import Scenario
from terminal_sim.yard import Yard

from terminal_tos_stub.allocator import SimpleSlotAllocator


def generate_workorders(
    scenario: Scenario,
    layout: Layout,
    allocator: SimpleSlotAllocator,
    rng: random.Random,
) -> list[WorkOrder]:
    """Deterministic work order list for ``scenario``.

    ``rng`` is accepted for future use (e.g. a 20/40 ft mix) but is currently
    NOT consumed: the sequence depends only on scenario, layout and allocator.
    """
    n = scenario.n_containers
    n_discharge = round(n * scenario.discharge_share)
    orders: list[WorkOrder] = []
    for i in range(n):
        block = layout.blocks[i % len(layout.blocks)].block
        slot = allocator.next_slot(block)
        if slot is None:
            raise ValueError("YARD_FULL")
        orders.append(
            WorkOrder(
                container_id=f"C{i + 1:04d}",
                type=JobType.DISCHARGE if i < n_discharge else JobType.LOAD,
                sts_id=layout.sts[i % scenario.n_sts].equipment_id,
                yard_slot=slot,
            )
        )
    return orders


def prefill(yard: Yard, orders: list[WorkOrder]) -> None:
    """Place the containers of all LOAD orders in the yard before the run."""
    for order in orders:
        if order.type is JobType.LOAD:
            yard.occupy(parse_location(order.yard_slot), order.container_id)
