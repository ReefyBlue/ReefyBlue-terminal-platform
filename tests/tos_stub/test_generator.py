import dataclasses
import random
from collections import Counter
from pathlib import Path

import pytest
from terminal_ecs.workorders import WorkOrder
from terminal_model.types import JobType
from terminal_runner.run import make_allocator, make_workorders
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_tos_stub.allocator import SimpleSlotAllocator
from terminal_tos_stub.generator import generate_workorders


def _gen(scenario, layout):
    return make_workorders(scenario, layout, make_allocator(layout), random.Random(scenario.seed))


def _setup():
    return load_scenario(Path("scenarios/baseline.yaml")), load_layout(Path("terminal.yaml"))


def _plain(n: int, share: float = 0.5):
    return generate_workorders(
        n_containers=n,
        discharge_share=share,
        sts_ids=["S1", "S2"],
        blocks=["X", "Y"],
        allocator=SimpleSlotAllocator({"X": (2, 1, 1), "Y": (2, 1, 1)}),
        rng=random.Random(0),
    )


def test_generator_takes_plain_arguments():
    assert [(o.container_id, o.type, o.sts_id, o.yard_slot) for o in _plain(4)] == [
        ("C0001", JobType.DISCHARGE, "S1", "X/1/1/1"),
        ("C0002", JobType.DISCHARGE, "S2", "Y/1/1/1"),
        ("C0003", JobType.LOAD, "S1", "X/2/1/1"),
        ("C0004", JobType.LOAD, "S2", "Y/2/1/1"),
    ]


def test_generator_raises_when_yard_full():
    with pytest.raises(ValueError, match="YARD_FULL"):
        _plain(5)


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


def _wo(cid, jtype, sts, slot):
    return WorkOrder(container_id=cid, type=jtype, sts_id=sts, yard_slot=slot)


D, L = JobType.DISCHARGE, JobType.LOAD


def test_baseline_order_list_unchanged_by_the_decoupling():
    # Captured from the generator before it took plain arguments (Scenario/Layout).
    scenario, layout = _setup()
    orders = _gen(scenario, layout)
    assert orders[:3] + orders[-3:] == [
        _wo("C0001", D, "STS-01", "A/1/1/1"),
        _wo("C0002", D, "STS-02", "B/1/1/1"),
        _wo("C0003", D, "STS-01", "C/1/1/1"),
        _wo("C0198", D, "STS-02", "B/3/1/2"),
        _wo("C0199", D, "STS-01", "C/3/1/2"),
        _wo("C0200", D, "STS-02", "D/3/1/2"),
    ]
    mixed = _gen(dataclasses.replace(scenario, discharge_share=0.5), layout)
    assert [o.type for o in mixed[-3:]] == [L, L, L]
    assert [o.yard_slot for o in mixed] == [o.yard_slot for o in orders]


def test_mixed_shares():
    scenario, layout = _setup()
    scenario = dataclasses.replace(scenario, discharge_share=0.5)
    orders = _gen(scenario, layout)
    dis = [o for o in orders if o.type is JobType.DISCHARGE]
    load = [o for o in orders if o.type is JobType.LOAD]
    assert (len(dis), len(load)) == (100, 100)
    assert {o.yard_slot for o in dis}.isdisjoint({o.yard_slot for o in load})
