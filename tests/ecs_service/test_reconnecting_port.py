from __future__ import annotations

import logging

from fake_port import FakePort
from terminal_ecs_service.port import ReconnectingPort
from terminal_model.port import EquipmentPort, EventKind, LinkReason
from terminal_model.types import Job, JobType, Kind, Position, State


def _fake() -> FakePort:
    return FakePort({"AGV-01": (Kind.AGV, Position(10, 50)), "STS-01": (Kind.STS, Position(100, 0))})


JOB = Job("J-1", JobType.TRANSPORT, "C1", "AGV-01", "STS-01")


def test_reconnecting_port_swaps_and_redelivers(caplog):
    rp = ReconnectingPort()
    seen = []
    rp.subscribe(seen.append)
    p1 = _fake()
    p1.advance(42.0)
    rp.bind(p1)
    assert rp.connected is True
    p1.set_state("AGV-01", State.BUSY)
    assert len(seen) == 1 and seen[0].kind is EventKind.STATE_CHANGED
    assert rp.snapshot("AGV-01").state is State.BUSY

    rp.unbind()
    assert rp.connected is False and rp.now() == p1.now() == 42.0
    assert rp.equipment_ids() == ["AGV-01", "STS-01"]
    assert rp.snapshot("AGV-01").state is State.OFFLINE  # unbind marks the cache OFFLINE
    with caplog.at_level(logging.WARNING):
        assert rp.assign_job("AGV-01", JOB) is False
        assert rp.cancel_job("AGV-01") is False
        assert rp.reset("AGV-01") is False
    assert p1.calls == [] and p1.calls_cancel == [] and p1.calls_reset == []
    assert sum("not connected" in r.message for r in caplog.records) == 3
    assert rp.drain_events() == 0

    n_lost = len(seen)
    p1.set_state("AGV-01", State.IDLE)  # stale port: not delivered any more
    assert len(seen) == n_lost

    p2 = _fake()
    n = len(seen)  # includes the LINK_LOST events of the unbind
    rp.bind(p2)  # LINK_RESTORED for every device
    assert [e.reason for e in seen[n:]] == ["LINK_RESTORED"] * 2
    p2.set_state("AGV-01", State.IDLE)
    assert len(seen) == n + 3 and seen[-1].reason is None
    assert rp.assign_job("AGV-01", JOB) is True and p2.calls == [("AGV-01", JOB)]


def test_late_subscriber_is_attached_to_the_bound_port():
    rp = ReconnectingPort()
    p = _fake()
    rp.bind(p)
    seen = []
    rp.subscribe(seen.append)
    p.set_state("STS-01", State.FAULT)
    assert [e.equipment_id for e in seen] == ["STS-01"]


def test_rebinding_the_same_port_does_not_duplicate_events():
    rp = ReconnectingPort()
    seen = []
    rp.subscribe(seen.append)
    p = _fake()
    rp.bind(p)
    rp.unbind()
    rp.bind(p)
    n = len(seen)  # LINK_LOST/LINK_RESTORED events
    p.set_state("AGV-01", State.BUSY)
    assert len(seen) == n + 1


def test_drain_events_delegates_when_the_port_has_it():
    class Draining(FakePort):
        def drain_events(self) -> int:
            return 7

    rp = ReconnectingPort()
    rp.bind(Draining({"AGV-01": (Kind.AGV, Position(0, 0))}))
    assert rp.drain_events() == 7
    rp.bind(_fake())
    assert rp.drain_events() == 0


def test_unbound_before_any_bind():
    rp = ReconnectingPort()
    assert rp.connected is False and rp.now() == 0.0 and rp.equipment_ids() == []


def test_reconnecting_port_satisfies_protocol():
    assert isinstance(ReconnectingPort(), EquipmentPort)


def test_first_bind_emits_nothing():
    rp = ReconnectingPort()
    seen = []
    rp.subscribe(seen.append)
    rp.bind(_fake())
    assert seen == []


def test_unbind_emits_offline_for_all_equipment():
    rp = ReconnectingPort()
    p = _fake()
    p.advance(42.0)
    seen = []
    snaps = []
    rp.subscribe(lambda ev: (seen.append(ev), snaps.append(rp.snapshot(ev.equipment_id).state)))
    rp.bind(p)
    p.set_state("AGV-01", State.BUSY)
    seen.clear()
    snaps.clear()
    rp.unbind()
    assert sorted(e.equipment_id for e in seen) == ["AGV-01", "STS-01"]
    for ev in seen:
        assert (ev.kind, ev.sim_time, ev.reason, ev.new_state) == (
            EventKind.STATE_CHANGED, 42.0, LinkReason.LINK_LOST, State.OFFLINE)
    assert snaps == [State.OFFLINE, State.OFFLINE]
    assert rp.snapshot("AGV-01").state is State.OFFLINE
    assert rp.snapshot("STS-01").state is State.OFFLINE
    rp.unbind()  # already unbound: no second round of events
    assert len(seen) == 2


def test_rebind_emits_link_restored_for_all():
    rp = ReconnectingPort()
    rp.bind(_fake())
    rp.unbind()
    p2 = _fake()
    p2.advance(300.0)
    p2.set_state("AGV-01", State.FAULT)
    seen = []
    snaps = []
    # a callback reading the snapshot (as the Dispatcher does) sees the fresh state
    rp.subscribe(lambda ev: (seen.append(ev), snaps.append(rp.snapshot(ev.equipment_id).state)))
    rp.bind(p2)
    by_id = {e.equipment_id: e for e in seen}
    assert len(seen) == 2 and set(by_id) == {"AGV-01", "STS-01"}  # also the unchanged one
    for ev in seen:
        assert (ev.kind, ev.sim_time, ev.reason) == (
            EventKind.STATE_CHANGED, 300.0, LinkReason.LINK_RESTORED)
    assert by_id["AGV-01"].new_state is State.FAULT
    assert by_id["STS-01"].new_state is State.IDLE
    assert sorted(snaps, key=lambda s: s.value) == sorted([State.FAULT, State.IDLE], key=lambda s: s.value)


def test_reconnecting_port_rack_occupancy():
    """Forwarded to the bound port (``RackOccupancyPort``), the last answer while unbound."""
    import pytest
    from terminal_model.port import RackOccupancyPort

    rp = ReconnectingPort()
    assert isinstance(rp, RackOccupancyPort)
    p = _fake()
    rp.bind(p)
    with pytest.raises(TypeError):  # a legacy port has no rack occupancy
        rp.rack_occupancy("A")
    occ = {"RK-A-1": "C1", "RK-A-2": None}
    p.rack_occupancy = lambda block: dict(occ) if block == "A" else {}[block]  # type: ignore[attr-defined]
    assert rp.rack_occupancy("A") == occ
    rp.unbind()
    occ["RK-A-1"] = None  # not seen while unbound
    assert rp.rack_occupancy("A") == {"RK-A-1": "C1", "RK-A-2": None}
    with pytest.raises(KeyError):
        rp.rack_occupancy("B")
