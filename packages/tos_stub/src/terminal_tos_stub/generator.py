from __future__ import annotations

import random
from collections.abc import Sequence

from terminal_ecs.workorders import SlotProvider, WorkOrder
from terminal_model.types import JobType


def generate_workorders(
    *,
    n_containers: int,
    discharge_share: float,
    sts_ids: Sequence[str],
    blocks: Sequence[str],
    allocator: SlotProvider,
    rng: random.Random,
) -> list[WorkOrder]:
    """Deterministic work order list: container i goes to ``blocks[i % len(blocks)]``
    and ``sts_ids[i % len(sts_ids)]``; the first ``round(n * discharge_share)`` are
    DISCHARGE, the rest LOAD.

    ``rng`` is accepted for future use (e.g. a 20/40 ft mix) but is currently
    NOT consumed: the sequence depends only on the arguments and the allocator.
    """
    if not sts_ids or not blocks:
        raise ValueError("generate_workorders needs at least one STS and one block")
    n_discharge = round(n_containers * discharge_share)
    orders: list[WorkOrder] = []
    for i in range(n_containers):
        slot = allocator.next_slot(blocks[i % len(blocks)])
        if slot is None:
            raise ValueError("YARD_FULL")
        orders.append(
            WorkOrder(
                container_id=f"C{i + 1:04d}",
                type=JobType.DISCHARGE if i < n_discharge else JobType.LOAD,
                sts_id=sts_ids[i % len(sts_ids)],
                yard_slot=slot,
            )
        )
    return orders
