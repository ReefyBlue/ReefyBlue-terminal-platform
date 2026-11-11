"""``/health`` extras, ``GET /containers`` and bounded retention in the ECS service
(Plan B2c Task 1)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
from fake_port import FakePort
from terminal_ecs.workorders import WorkOrder
from terminal_ecs_service import core as core_mod
from terminal_ecs_service.app import create_app
from terminal_ecs_service.core import KNOWN_RETENTION, SAMPLE_INTERVAL_S, EcsCore
from terminal_model.types import JobType, Kind, Position, State

LAYOUT = SimpleNamespace(blocks=[SimpleNamespace(block="A", bays=40, rows=4, tiers=4)])


def _fake() -> FakePort:
    return FakePort(
        {
            "STS-01": (Kind.STS, Position(100, 0)),
            "AGV-01": (Kind.AGV, Position(10, 50)),
            "ASC-A": (Kind.ASC, Position(50, 120)),
        }
    )


def _setup(**kw):
    fake = _fake()
    core = EcsCore(LAYOUT, connect=lambda: fake, **kw)
    app = create_app(core, run_loop=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return fake, core, client


def _slot(i: int) -> str:
    return f"A/{1 + i // 16}/{1 + (i // 4) % 4}/{1 + i % 4}"


def _order(cid: str, i: int) -> dict:
    return {"container_id": cid, "type": "DISCHARGE", "sts_id": "STS-01", "yard_slot": _slot(i)}


def _run_until_done(fake: FakePort, core: EcsCore, n: int) -> None:
    while core.dispatcher.status()["done"] < n:
        busy = [e for e in fake.equipment_ids() if fake.snapshot(e).state is State.BUSY]
        assert busy, "no progress"
        fake.complete(busy[0])


async def test_health_has_rss_and_uptime():
    _, core, client = _setup()
    async with client:
        r = await client.get("/health")
        assert r.status_code == 503
        body = r.json()
        assert body["connected"] is False and body["rss_mb"] > 0 and body["uptime_s"] >= 0
        await core.connect_once()
        r = await client.get("/health")
        body = r.json()
        assert r.status_code == 200 and body["connected"] is True and body["sim_time"] == 0.0
        assert isinstance(body["rss_mb"], float) and body["rss_mb"] > 0
        assert isinstance(body["uptime_s"], float) and body["uptime_s"] >= 0


async def test_containers_endpoint_pages(monkeypatch):
    _, core, client = _setup()
    async with client:
        r = await client.get("/containers")
        assert r.status_code == 200 and r.json() == {"next": 0, "items": []}  # before connecting
        await core.connect_once()
        n = 520
        for start in range(0, n, 130):
            r = await client.post("/workorders", json=[_order(f"C{i:03d}", i) for i in range(start, start + 130)])
            assert r.status_code == 202
        r = await client.get("/containers")
        page1 = r.json()
        assert r.status_code == 200 and len(page1["items"]) == 500
        first = page1["items"][0]
        assert first == {"seq": first["seq"], "container_id": "C000", "state": "open",
                         "job_type": "DISCHARGE", "yard_slot": "A/1/1/1"}
        seqs = [it["seq"] for it in page1["items"]]
        assert seqs == sorted(seqs) and page1["next"] == seqs[-1]
        page2 = (await client.get("/containers", params={"since": page1["next"]})).json()
        assert [it["container_id"] for it in page2["items"]] == [f"C{i:03d}" for i in range(500, 520)]
        page3 = (await client.get("/containers", params={"since": page2["next"]})).json()
        assert page3 == {"next": page2["next"], "items": []}
        assert (await client.get("/containers", params={"since": -1})).status_code == 422
        assert (await client.get("/containers", params={"since": "x"})).status_code == 422


async def test_step_prunes_finished_and_keeps_counting(monkeypatch):
    fake, core, client = _setup(known_retention=3)
    async with client:
        await core.connect_once()
        r = await client.post("/workorders", json=[_order(f"C{i}", i) for i in range(8)])
        assert r.status_code == 202
        _run_until_done(fake, core, 6)
        assert len(core.dispatcher.completed) == 6  # nothing pruned between boundaries
        fake.advance(SAMPLE_INTERVAL_S)
        core.step()
        d = core.dispatcher
        assert len(d.completed) == 3 and d.status()["done"] == 6
        assert core.kpis()["containers_done"] == 6.0  # KPI stays cumulative
        assert not core.is_known("C0") and core.is_known("C5") and core.is_known("C7")
        assert core._seen_jobs <= set(d.job_log)  # published-job memory pruned alongside
        # a pruned container id may be submitted again; a retained or open one may not
        r = await client.post("/workorders", json=[_order("C0", 20)])
        assert r.status_code == 202
        r = await client.post("/workorders", json=[_order("C5", 21)])
        assert r.status_code == 422 and r.json()["errors"][0]["code"] == "DUPLICATE_CONTAINER"
        r = await client.post("/workorders", json=[_order("C7", 22)])
        assert r.status_code == 422
        items = (await client.get("/containers")).json()["items"]
        assert [(it["container_id"], it["state"]) for it in items] == [
            ("C6", "open"), ("C7", "open"), ("C3", "done"), ("C4", "done"), ("C5", "done"), ("C0", "open"),
        ]


def test_known_retention_default():
    assert KNOWN_RETENTION == 10_000
    core = EcsCore(LAYOUT, connect=_fake)
    assert core.known_retention == KNOWN_RETENTION
    assert core.is_known("C1") is False  # no dispatcher yet


async def test_is_known_tracks_submit():
    fake = _fake()
    core = EcsCore(LAYOUT, connect=lambda: fake)
    await core.connect_once()
    core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])
    assert core.is_known("C1") and not core.is_known("C2")
    assert not hasattr(core, "known_containers")
    assert core_mod.KNOWN_RETENTION == 10_000
