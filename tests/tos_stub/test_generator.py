import dataclasses
import random
from collections import Counter
from pathlib import Path

from terminal_model.types import JobType
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_tos_stub.allocator import SimpleSlotAllocator
from terminal_tos_stub.generator import generate_workorders


def _gen(scenario, layout):
    return generate_workorders(
        scenario, layout, SimpleSlotAllocator(layout.blocks), random.Random(scenario.seed)
    )


def _setup():
    return load_scenario(Path("scenarios/baseline.yaml")), load_layout(Path("terminal.yaml"))


def test_generator_counts_and_roundrobin():
    scenario, layout = _setup()
    orders = _gen(scenario, layout)
    assert len(orders) == 200
    assert orders[0].container_id == "C0001"
    assert orders[-1].container_id == "C0200"
    assert Counter(o.sts_id for o in orders) == {"STS-01": 100, "STS-02": 100}
    assert Counter(o.yard_slot[0] for o in orders) == {"A": 50, "B": 50, "C": 50, "D": 50}
    assert all(o.type is JobType.DISCHARGE for o in orders)
    assert len({o.yard_slot for o in orders}) == 200


def test_generator_deterministic():
    scenario, layout = _setup()
    assert _gen(scenario, layout) == _gen(scenario, layout)


def test_mixed_shares():
    scenario, layout = _setup()
    scenario = dataclasses.replace(scenario, discharge_share=0.5)
    orders = _gen(scenario, layout)
    dis = [o for o in orders if o.type is JobType.DISCHARGE]
    load = [o for o in orders if o.type is JobType.LOAD]
    assert (len(dis), len(load)) == (100, 100)
    assert {o.yard_slot for o in dis}.isdisjoint({o.yard_slot for o in load})
