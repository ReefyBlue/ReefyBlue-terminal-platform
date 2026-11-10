from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fake_port import FakePort
from fastapi.testclient import TestClient
from terminal_ecs_service import serve
from terminal_ecs_service.app import create_app
from terminal_ecs_service.core import EcsCore
from terminal_model.types import Kind, Position, State

ROOT = Path(__file__).resolve().parents[2]
LAYOUT = SimpleNamespace(blocks=[SimpleNamespace(block="A", bays=2, rows=2, tiers=2)])


def _fake(cls=FakePort) -> FakePort:
    return cls(
        {
            "STS-01": (Kind.STS, Position(100, 0)),
            "AGV-01": (Kind.AGV, Position(10, 50)),
            "ASC-A": (Kind.ASC, Position(50, 120)),
        }
    )


def _setup(fake=None):
    fake = fake if fake is not None else _fake()
    core = EcsCore(LAYOUT, connect=lambda: fake)
    app = create_app(core, run_loop=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return fake, core, client


def _order(cid: str, slot: str = "A/1/1/1", sts: str = "STS-01", type_: str = "DISCHARGE") -> dict:
    return {"container_id": cid, "type": type_, "sts_id": sts, "yard_slot": slot}


async def test_health_503_then_200():
    _, core, client = _setup()
    async with client:
        r = await client.get("/health")
        assert r.status_code == 503 and r.json() == {"connected": False, "sim_time": None}
        await core.connect_once()
        r = await client.get("/health")
        assert r.status_code == 200 and r.json() == {"connected": True, "sim_time": 0.0}


async def test_workorders_accepted_202():
    _, core, client = _setup()
    async with client:
        await core.connect_once()
        r = await client.post("/workorders", json=[_order("C1")])
        assert r.status_code == 202 and r.json() == {"accepted": 1}
        st = (await client.get("/status")).json()
        assert st["open"] == 1 and st["connected"] is True and "tick_count" in st and "uptime_s" in st


async def test_workorders_batch_rejected_atomically():
    _, core, client = _setup()
    async with client:
        await core.connect_once()
        batch = [_order("C1", "A/1/1/1"), _order("C2", "A/1/1/2"), _order("C1", "A/1/2/1"),
                 _order("C4", "A/1/2/2"), _order("C5", "A/2/1/1")]
        r = await client.post("/workorders", json=batch)
        assert r.status_code == 422
        errors = r.json()["errors"]
        assert [(e["container_id"], e["code"]) for e in errors] == [("C1", "DUPLICATE_CONTAINER")]
        assert errors[0]["message"]
        assert (await client.get("/status")).json()["open"] == 0
        assert core.allocator.next_slot("A") == "A/1/1/1"  # nothing occupied


async def test_workorders_duplicate_against_earlier_batch():
    _, core, client = _setup()
    async with client:
        await core.connect_once()
        assert (await client.post("/workorders", json=[_order("C1")])).status_code == 202
        r = await client.post("/workorders", json=[_order("C1", "A/1/1/2")])
        assert r.status_code == 422
        assert [e["code"] for e in r.json()["errors"]] == ["DUPLICATE_CONTAINER"]


async def test_workorders_invalid_slot_422():
    _, core, client = _setup()
    async with client:
        await core.connect_once()
        r = await client.post("/workorders", json=[_order("C1", "Z/1/1/1"), _order("C2", "garbage"),
                                                   _order("C3", "TP/A/WS")])
        assert r.status_code == 422
        assert [(e["container_id"], e["code"]) for e in r.json()["errors"]] == [
            ("C1", "INVALID_SLOT"), ("C2", "INVALID_SLOT"), ("C3", "INVALID_SLOT")]
        assert core.allocator.next_slot("A") == "A/1/1/1"


async def test_workorders_unknown_sts_422():
    _, core, client = _setup()
    async with client:
        await core.connect_once()
        r = await client.post("/workorders", json=[_order("C1", sts="STS-99"), _order("C2", sts="AGV-01")])
        assert r.status_code == 422
        assert [(e["container_id"], e["code"]) for e in r.json()["errors"]] == [
            ("C1", "UNKNOWN_STS"), ("C2", "UNKNOWN_STS")]


async def test_workorders_bad_type_422_in_errors_format():
    _, core, client = _setup()
    async with client:
        await core.connect_once()
        r = await client.post("/workorders", json=[_order("C1"), _order("C2", type_="STACK_IN")])
        assert r.status_code == 422
        errors = r.json()["errors"]
        assert [(e["container_id"], e["code"]) for e in errors] == [("C2", "INVALID_ORDER")]
        assert (await client.get("/status")).json()["open"] == 0


async def test_workorders_503_without_connection():
    _, _, client = _setup()
    async with client:
        r = await client.post("/workorders", json=[_order("C1")])
        assert r.status_code == 503


class LosingPort(FakePort):
    lost = False
    closed = 0

    def snapshot(self, equipment_id):
        if self.lost:
            raise ConnectionError("link lost")
        return super().snapshot(equipment_id)

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("method,path", [("GET", "/status"), ("GET", "/kpis"), ("GET", "/equipment"),
                                         ("POST", "/workorders")])
async def test_connection_error_in_endpoint_drops_link_and_503(method, path):
    fake, core, client = _setup(_fake(LosingPort))
    async with client:
        await core.connect_once()
        fake.lost = True
        r = await client.request(method, path, json=[_order("C1")] if method == "POST" else None)
        assert r.status_code == 503
        assert core.connected is False
        assert (await client.get("/health")).status_code == 503


async def test_kpis_before_first_tick_and_without_connection():
    _, core, client = _setup()
    async with client:
        assert (await client.get("/kpis")).status_code == 503
        await core.connect_once()
        r = await client.get("/kpis")
        assert r.status_code == 200
        k = r.json()
        assert k["containers_done"] == 0.0 and k["sim_elapsed_s"] == 0.0


async def test_equipment_lists_fleet():
    _, core, client = _setup()
    async with client:
        assert (await client.get("/equipment")).json() == []
        await core.connect_once()
        eq = (await client.get("/equipment")).json()
        assert sorted((e["equipment_id"], e["kind"]) for e in eq) == [
            ("AGV-01", "AGV"), ("ASC-A", "ASC"), ("STS-01", "STS")]
        agv = next(e for e in eq if e["equipment_id"] == "AGV-01")
        assert agv["position"] == {"x": 10, "y": 50} and agv["state"] == "IDLE"


def test_ws_first_message_is_status_then_events():
    fake = _fake()
    core = EcsCore(LAYOUT, connect=lambda: fake)
    app = create_app(core, run_loop=False)
    with TestClient(app) as client:
        client.portal.call(core.connect_once)
        with client.websocket_connect("/events") as ws:
            first = ws.receive_json()
            assert first["type"] == "status" and first["open"] == 0 and first["sim_time"] == 0.0
            assert first["equipment"] == {"AGV-01": "IDLE", "ASC-A": "IDLE", "STS-01": "IDLE"}

            def fault():
                fake.advance(5)
                fake.set_state("ASC-A", State.FAULT)
                core.step()

            client.portal.call(fault)
            msg = ws.receive_json()
            assert msg["type"] == "equipment" and msg["kind"] == "STATE_CHANGED"
            assert msg["equipment_id"] == "ASC-A" and msg["new_state"] == "FAULT"
        assert core.broadcaster.clients == 0  # disconnect -> unregistered


def test_ws_status_before_connection():
    core = EcsCore(LAYOUT, connect=lambda: _fake())
    with TestClient(create_app(core, run_loop=False)) as client, client.websocket_connect("/events") as ws:
        first = ws.receive_json()
        assert first["type"] == "status" and first["open"] == 0 and first["equipment"] == {}


def test_ws_link_messages_on_reconnect():
    fake = _fake()
    core = EcsCore(LAYOUT, connect=lambda: fake)
    app = create_app(core, run_loop=False)
    with TestClient(app) as client:
        client.portal.call(core.connect_once)
        with client.websocket_connect("/events") as ws:
            assert ws.receive_json()["type"] == "status"
            client.portal.call(core.on_link_lost)
            assert ws.receive_json() == {"type": "link", "connected": False, "sim_time": 0.0}
            client.portal.call(core.connect_once)
            assert ws.receive_json() == {"type": "link", "connected": True, "sim_time": 0.0}


def test_lifespan_runs_and_cancels_core_loop():
    fake = _fake()
    core = EcsCore(LAYOUT, connect=lambda: fake)
    with TestClient(create_app(core)) as client:
        for _ in range(200):
            if client.get("/health").status_code == 200:
                break
            client.portal.call(__import__("anyio").sleep, 0.01)
        assert client.get("/health").status_code == 200
    assert core.connected is False  # run() cancelled -> link dropped


def test_serve_main_builds_app(monkeypatch):
    calls = []
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setattr(serve, "configure_json_logging", lambda level: calls.append(("log", level)))
    rc = serve.main(["--gateway", "opc.tcp://x", "--layout", str(ROOT / "terminal.yaml"), "--port", "1"])
    assert rc == 0
    assert calls[0] == ("log", 20)
    app, kw = calls[1]
    assert kw["host"] == "0.0.0.0" and kw["port"] == 1 and kw["log_config"] is None
    assert kw["log_level"] == "info"
    assert {r.path for r in app.routes} >= {"/workorders", "/status", "/kpis", "/equipment", "/health",
                                            "/events"}


def test_serve_load_layout_and_factory(monkeypatch):
    layout = serve.load_layout(ROOT / "terminal.yaml")
    assert [b.block for b in layout.blocks] == ["A", "B", "C", "D"]
    assert (layout.blocks[0].bays, layout.blocks[0].rows, layout.blocks[0].tiers) == (40, 6, 4)

    made = []

    class StubPort:
        def __init__(self, endpoint):
            self.endpoint = endpoint
            made.append(self)

        def connect(self):
            raise ConnectionError("down")

    monkeypatch.setattr(serve, "OpcUaEquipmentPort", StubPort)
    factory = serve.port_factory("opc.tcp://gw:4840")
    with pytest.raises(ConnectionError):
        factory()
    assert made[0].endpoint == "opc.tcp://gw:4840"


class TickLosingPort(FakePort):
    lost = False

    def assign_job(self, equipment_id, job):
        if self.lost:
            raise ConnectionError("link lost")
        return super().assign_job(equipment_id, job)


async def test_workorders_202_when_link_drops_after_take():
    fake, core, client = _setup(_fake(TickLosingPort))
    async with client:
        await core.connect_once()
        fake.lost = True  # validation reads snapshots fine; the tick's assign_job fails
        r = await client.post("/workorders", json=[_order("C1"), _order("C2", "A/1/1/2")])
        assert r.status_code == 202 and r.json() == {"accepted": 2}
        assert core.known_containers == {"C1", "C2"}
        assert core.dispatcher.status()["open"] == 2
        assert (await client.get("/health")).status_code == 503
        r = await client.post("/workorders", json=[_order("C1")])
        assert r.status_code == 503  # not connected; after reconnect it would be a duplicate
