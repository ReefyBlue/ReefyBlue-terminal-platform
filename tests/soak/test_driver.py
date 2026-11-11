"""Soak work driver against the in-process ECS service (Plan B2c Task 5)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fake_port import FakePort
from starlette.testclient import TestClient
from terminal_ecs_service import app as app_mod
from terminal_ecs_service.app import create_app
from terminal_ecs_service.core import EcsCore
from terminal_model.types import JobType, Kind, Position, State
from terminal_soak import driver as driver_mod
from terminal_soak.driver import Driver
from terminal_tos_stub.client import EcsClient

LAYOUT = SimpleNamespace(
    blocks=[SimpleNamespace(block="A", asc_id="ASC-A", bays=20, rows=2, tiers=2)],
    sts=[SimpleNamespace(equipment_id="STS-01")],
)
T0 = 1_800_000_000.0  # wall epoch seconds


def _fake() -> FakePort:
    return FakePort({
        "STS-01": (Kind.STS, Position(100, 0)),
        "AGV-01": (Kind.AGV, Position(10, 50)),
        "ASC-A": (Kind.ASC, Position(50, 120)),
    })


@contextlib.contextmanager
def ecs(monkeypatch: pytest.MonkeyPatch | None = None, *, ui: bool = True) -> Iterator[SimpleNamespace]:
    if not ui:
        assert monkeypatch is not None
        monkeypatch.setattr(app_mod, "_viewer_dir", lambda: None)
    fake = _fake()
    core = EcsCore(LAYOUT, connect=lambda: fake)
    app = create_app(core, run_loop=False)
    with TestClient(app) as tc:
        tc.portal.call(core.connect_once)
        yield SimpleNamespace(fake=fake, core=core, client=EcsClient(http=tc), portal=tc.portal)


def _slot(i: int) -> str:
    return f"A/{1 + i // 4}/{1 + (i // 2) % 2}/{1 + i % 2}"


def _post(e: SimpleNamespace, cids: list[str], start: int = 0, kind: str = "DISCHARGE") -> None:
    from terminal_ecs.workorders import WorkOrder
    e.client.post_workorders([WorkOrder(c, JobType(kind), "STS-01", _slot(start + i)) for i, c in enumerate(cids)])


def _complete(e: SimpleNamespace, n: int) -> None:
    """Run the fake fleet until ``n`` orders are done."""
    while e.core.dispatcher.status()["done"] < n:
        busy = [q for q in e.fake.equipment_ids() if e.fake.snapshot(q).state is State.BUSY]
        assert busy, "no progress"
        e.portal.call(e.fake.complete, busy[0])
        e.portal.call(e.core.step)


def _driver(client, tmp_path, **kw) -> Driver:
    kw.setdefault("low_water", 40)
    kw.setdefault("batch", 6)
    return Driver(client, LAYOUT, out_dir=tmp_path, **kw)


class NoClient:
    def __getattr__(self, name):
        raise AssertionError(f"make_batch must not call the ECS ({name})")


def test_make_batch_discharge_only_on_empty_yard(tmp_path):
    d = _driver(NoClient(), tmp_path)
    orders = d.make_batch()
    assert [o.container_id for o in orders] == [f"SOAK-1-{i}" for i in range(6)]
    assert {o.type for o in orders} == {JobType.DISCHARGE}
    assert len({o.yard_slot for o in orders}) == 6 and all(o.sts_id == "STS-01" for o in orders)
    assert d.yard.occupied_count == 6 and d.yard.loadable_count == 0
    # still discharge-only while fewer than batch//2 slots are taken
    d2 = _driver(NoClient(), tmp_path)
    d2.yard.occupy("A/1/1/1", "X1")
    d2.yard.occupy("A/1/1/2", "X2")
    assert {o.type for o in d2.make_batch()} == {JobType.DISCHARGE}


def test_make_batch_alternates_when_yard_has_boxes(tmp_path):
    d = _driver(NoClient(), tmp_path)
    stored = {_slot(i) for i in range(5)}
    for i, s in enumerate(sorted(stored)):
        d.yard.occupy(s, f"X{i}")
    orders = d.make_batch()
    assert [o.type for o in orders] == [JobType.DISCHARGE, JobType.LOAD] * 3
    loads = [o.yard_slot for o in orders if o.type is JobType.LOAD]
    discharges = [o.yard_slot for o in orders if o.type is JobType.DISCHARGE]
    assert set(loads) <= stored and len(set(loads)) == 3  # LOAD slots are occupied ones
    assert not set(discharges) & stored  # DISCHARGE slots are free ones
    assert d.yard.loadable_count == 2
    # second batch: ids continue, LOADs only from what is still loadable, then DISCHARGE
    orders2 = d.make_batch()
    assert orders2[0].container_id == "SOAK-2-0"
    assert [o.type for o in orders2] == [JobType.DISCHARGE, JobType.LOAD, JobType.DISCHARGE, JobType.LOAD,
                                         JobType.DISCHARGE, JobType.DISCHARGE]
    assert d.yard.loadable_count == 0


def test_driver_submits_below_low_water(tmp_path):
    with ecs() as e:
        _post(e, [f"P{i}" for i in range(40)])
        d = _driver(e.client, tmp_path)
        d.resume()
        d.tick(T0)
        assert e.core.dispatcher.status()["open"] == 40  # open 40: nothing posted
    with ecs() as e:
        _post(e, [f"P{i}" for i in range(39)])
        d = _driver(e.client, tmp_path)
        d.resume()
        d.tick(T0)
        st = e.core.dispatcher.status()
        assert st["open"] == 45 and d.batches_posted == 1
        assert e.core.is_known("SOAK-1-0") and e.core.is_known("SOAK-1-5")
        # the driver's discharges avoid the slots of the 39 orders it learnt from /containers
        assert d.yard.occupied_count == 45
        d.tick(T0 + 10)
        assert e.core.dispatcher.status()["open"] == 45  # 45 >= 40: no second batch


def test_driver_resumes_from_containers(tmp_path):
    with ecs() as e:
        _post(e, [f"D{i}" for i in range(20)])
        _complete(e, 8)
        assert e.core.dispatcher.status()["open"] == 12
        d = _driver(e.client, tmp_path)
        d.resume()
        assert d.yard.occupied_count == 20 and d.yard.loadable_count == 8
        # LOAD 3 boxes out and finish everything
        orders = d.make_batch()
        loads = [o for o in orders if o.type is JobType.LOAD]
        assert loads and orders[0].container_id == "SOAK-1-0"
        e.client.post_workorders(orders)
        n_done = 8
        _complete(e, n_done + 12 + len(orders))
        d2 = _driver(e.client, tmp_path)
        d2.resume()
        assert d2.yard.occupied_count == 20 + (len(orders) - len(loads)) - len(loads)
        assert d2.make_batch()[0].container_id == "SOAK-2-0"  # batch numbers survive a driver restart


def test_resume_pages_and_restarts_from_zero_when_cursor_regresses(tmp_path):
    pages = {
        0: {"next": 2, "items": [
            {"seq": 1, "container_id": "SOAK-3-0", "state": "done", "job_type": "DISCHARGE", "yard_slot": "A/1/1/1"},
            {"seq": 2, "container_id": "SOAK-3-1", "state": "open", "job_type": "DISCHARGE", "yard_slot": "A/1/1/2"},
        ]},
        2: {"next": 2, "items": []},
    }
    calls = []

    class Stub:
        def containers(self, since):
            calls.append(since)
            return pages[since] if since in pages else {"next": 0, "items": []}

    d = _driver(Stub(), tmp_path)
    d._poll_containers()
    assert calls == [0] and d.yard.occupied_count == 2
    d._poll_containers()
    assert calls == [0, 2]
    d._cursor = 50  # e.g. the ECS restarted: its answer has next < since
    d._poll_containers()
    assert calls == [0, 2, 50, 0] and d.ecs_resets_seen == 1
    assert d.make_batch()[0].container_id == "SOAK-4-0"


def test_tick_detects_cuts_and_recovery(tmp_path):
    with ecs() as e:
        d = _driver(e.client, tmp_path, low_water=0)
        d.resume()
        d.tick(T0)
        e.portal.call(e.fake.set_state, "AGV-01", State.OFFLINE, "LINK_LOST")
        e.portal.call(e.core.step)
        d.tick(T0 + 10)
        d.tick(T0 + 20)
        assert d.cuts_seen == 1 and d.recoveries == []
        e.portal.call(e.fake.set_state, "AGV-01", State.IDLE)
        e.portal.call(e.core.step)
        d.tick(T0 + 40)
        assert d.cuts_seen == 1
        assert d.recoveries == [{"at": "2027-01-15T08:00:10+00:00", "recovered_s": 30.0}]


def test_sample_line_fields(tmp_path, monkeypatch):
    with ecs(monkeypatch, ui=False) as e:
        d = _driver(e.client, tmp_path)
        d.resume()
        d.tick(T0)
        d.sample(T0 + 5)
    monkeypatch.undo()  # the viewer is mounted again
    with ecs() as e:
        d2 = _driver(e.client, tmp_path)
        d2.sample(T0 + 6)
    lines = [json.loads(x) for x in (tmp_path / "samples.jsonl").read_text().splitlines()]
    assert len(lines) == 2
    s = lines[0]
    for key in ("wall_time", "sim_time", "status", "kpis", "health", "alarms_by_kind", "ui_ok",
                "cuts_seen", "recoveries"):
        assert key in s, key
    assert s["wall_time"] == "2027-01-15T08:00:05+00:00" and s["sim_time"] == 0.0
    assert s["status"]["open"] == 6 and "done" in s["status"]
    assert "moves_per_hour_total" in s["kpis"]
    assert set(s["health"]) == {"rss_mb", "uptime_s", "connected"} and s["health"]["connected"] is True
    assert s["health"]["rss_mb"] > 0
    assert s["alarms_by_kind"] == {} and s["cuts_seen"] == 0 and s["recoveries"] == []
    assert s["ui_ok"] is False  # /ui/ answers 404
    assert lines[1]["ui_ok"] is True


def test_sample_survives_unreachable_ecs(tmp_path):
    class Down:
        def __getattr__(self, name):
            def fail(*a, **k):
                import httpx
                raise httpx.ConnectError("refused")
            return fail

    d = _driver(Down(), tmp_path)
    d.tick(T0)  # no exception
    d.sample(T0)
    s = json.loads((tmp_path / "samples.jsonl").read_text())
    assert s["status"] == {} and s["kpis"] == {} and s["ui_ok"] is False
    assert s["health"] == {"rss_mb": None, "uptime_s": None, "connected": False}


def test_run_samples_at_start_and_stop(tmp_path):
    with ecs() as e:
        d = _driver(e.client, tmp_path, loop_s=0.01, sample_every_s=3600)
        d.run(hours=0.03 / 3600)
        lines = (tmp_path / "samples.jsonl").read_text().splitlines()
        assert len(lines) == 2 and d.batches_posted >= 1


def test_main_parses_args(tmp_path, monkeypatch):
    seen = {}

    class FakeDriver:
        def __init__(self, client, layout, **kw):
            seen.update(kw, layout=layout)

        def run(self, hours):
            seen["hours"] = hours
            return 0

        def stop(self):
            pass

    monkeypatch.setattr(driver_mod, "Driver", FakeDriver)
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    rc = driver_mod.main(["--ecs", "http://ecs:8000", "--layout", str(root / "terminal.yaml"), "--hours", "0.5",
                          "--out", str(tmp_path), "--low-water", "20", "--batch", "30", "--loop", "2",
                          "--sample-every", "60"])
    assert rc == 0
    assert seen["hours"] == 0.5 and seen["low_water"] == 20 and seen["batch"] == 30
    assert seen["loop_s"] == 2.0 and seen["sample_every_s"] == 60.0 and seen["out_dir"] == tmp_path
    assert [b.block for b in seen["layout"].blocks] == ["A", "B", "C", "D"]
