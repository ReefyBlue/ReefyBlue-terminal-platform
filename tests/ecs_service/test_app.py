from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fake_port import FakePort
from fastapi.testclient import TestClient
from terminal_ecs.workorders import WorkOrder
from terminal_ecs_service import serve
from terminal_ecs_service.app import create_app
from terminal_ecs_service.core import EcsCore
from terminal_model.types import JobType, Kind, Position, State

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
        assert r.status_code == 503 and r.json().items() >= {"connected": False, "sim_time": None}.items()
        await core.connect_once()
        r = await client.get("/health")
        assert r.status_code == 200 and r.json().items() >= {"connected": True, "sim_time": 0.0}.items()
        assert "error" not in r.json() and set(r.json()) == {"connected", "sim_time", "rss_mb", "uptime_s"}


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

            def next_link() -> dict:
                # LINK_LOST/LINK_RESTORED equipment events (one per device) precede/follow it
                while (msg := ws.receive_json())["type"] != "link":
                    if msg["type"] == "alarm":  # LINK_LOST raised/cleared per device
                        assert msg["kind"] == "LINK_LOST"
                        continue
                    assert msg["type"] == "equipment" and msg["kind"] == "STATE_CHANGED"
                return msg

            client.portal.call(core.on_link_lost)
            assert next_link() == {"type": "link", "connected": False, "sim_time": 0.0}
            client.portal.call(core.connect_once)
            assert next_link() == {"type": "link", "connected": True, "sim_time": 0.0}


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
    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(signal, "signal", lambda sig, h: installed.append((sig, h)))
    installed = []
    monkeypatch.setattr(serve, "configure_json_logging", lambda level: calls.append(("log", level)))
    rc = serve.main(["--gateway", "opc.tcp://x", "--layout", str(ROOT / "terminal.yaml"), "--port", "1"])
    assert rc == 0
    assert signal.getsignal(signal.SIGTERM) is previous
    (sig, handler), = installed
    assert sig == signal.SIGTERM
    with pytest.raises(SystemExit) as ei:
        handler(signal.SIGTERM, None)
    assert ei.value.code == 0
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
        assert core.is_known("C1") and core.is_known("C2")
        assert core.dispatcher.status()["open"] == 2
        assert (await client.get("/health")).status_code == 503
        r = await client.post("/workorders", json=[_order("C1")])
        assert r.status_code == 503  # not connected; after reconnect it would be a duplicate


def test_openapi_declares_workorder_errors_and_health_503():
    _, core, _ = _setup()
    spec = create_app(core, run_loop=False).openapi()
    wo = spec["paths"]["/workorders"]["post"]["responses"]
    ref = "#/components/schemas/WorkOrderErrorsOut"
    assert wo["422"]["content"]["application/json"]["schema"] == {"$ref": ref}
    assert wo["500"]["content"]["application/json"]["schema"] == {"$ref": ref}
    assert "503" in wo and "202" in wo
    health = spec["paths"]["/health"]["get"]["responses"]
    assert set(health) >= {"200", "503"}
    assert "error" in spec["components"]["schemas"]["HealthOut"]["properties"]


# -- B2d: alarms, release, resolve (spec §6) ----------------------------------------------


async def test_alarms_endpoint_lists_sorted():
    fake, core, client = _setup()
    async with client:
        assert (await client.get("/alarms")).json() == []  # before the first connection
        await core.connect_once()
        fake.advance(10)
        fake.set_state("ASC-A", State.FAULT)
        fake.set_state("AGV-01", State.FAULT)
        fake.advance(5)
        fake.set_state("STS-01", State.FAULT)
        r = await client.get("/alarms")
        assert r.status_code == 200
        alarms = r.json()
        assert [(a["kind"], a["equipment_id"], a["since_sim_time"]) for a in alarms] == [
            ("NEEDS_RELEASE", "AGV-01", 10.0), ("NEEDS_RELEASE", "ASC-A", 10.0),
            ("NEEDS_RELEASE", "STS-01", 15.0)]
        assert set(alarms[0]) == {"kind", "equipment_id", "job_id", "since_sim_time", "detail"}
        assert alarms[0]["job_id"] is None and isinstance(alarms[0]["detail"], str)
        core.on_link_lost()  # LINK_LOST alarms at sim time 15 sort after the NEEDS_RELEASE at 15
        kinds = [(a["kind"], a["since_sim_time"]) for a in (await client.get("/alarms")).json()]
        assert kinds[2:] == [("LINK_LOST", 15.0)] * 3 + [("NEEDS_RELEASE", 15.0)]


async def test_release_409_while_fault_then_200():
    fake, core, client = _setup()
    async with client:
        await core.connect_once()
        fake.set_state("AGV-01", State.FAULT)
        r = await client.post("/equipment/AGV-01/release")
        assert r.status_code == 409
        assert r.json() == {"code": "RELEASE_REFUSED", "message": "equipment reports FAULT"}
        assert [(a["kind"], a["equipment_id"]) for a in (await client.get("/alarms")).json()] == [
            ("NEEDS_RELEASE", "AGV-01")]
        fake.set_state("AGV-01", State.IDLE)
        r = await client.post("/equipment/AGV-01/release")
        assert r.status_code == 200 and r.json() == {"released": "AGV-01"}
        assert (await client.get("/alarms")).json() == []


async def test_release_404_unknown():
    _, core, client = _setup()
    async with client:
        await core.connect_once()
        r = await client.post("/equipment/AGV-99/release")
        assert r.status_code == 404
        body = r.json()
        assert body["code"] == "UNKNOWN_EQUIPMENT" and "AGV-99" in body["message"]


async def _unknown_outcome() -> tuple[EcsCore, httpx.AsyncClient, str]:
    """A core whose AGV-01 came back IDLE without its job after a reconnect."""
    first, second = _fake(LosingPort), _fake()
    ports = [first, second]
    core = EcsCore(LAYOUT, connect=lambda: ports.pop(0))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(core, run_loop=False)),
                               base_url="http://t")
    await core.connect_once()
    core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])
    job_id = first.calls[0][1].job_id
    assert first.calls[0][0] == "AGV-01"
    core.on_link_lost()
    assert await core.connect_once() is True
    return core, client, job_id


@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_resolve_200_completed_and_failed(outcome):
    core, client, job_id = await _unknown_outcome()
    async with client:
        assert any(a["kind"] == "UNKNOWN_OUTCOME" for a in (await client.get("/alarms")).json())
        # release is refused while the outcome is open
        r = await client.post("/equipment/AGV-01/release")
        assert r.status_code == 409 and r.json() == {"code": "RELEASE_REFUSED",
                                                     "message": f"resolve job {job_id} first"}
        r = await client.post(f"/jobs/{job_id}/resolve", json={"outcome": outcome})
        assert r.status_code == 200 and r.json() == {"job_id": job_id, "outcome": outcome}
        assert core.status()["unknown_outcomes"] == 0
        assert [a["kind"] for a in (await client.get("/alarms")).json()] == ["NEEDS_RELEASE"]
        assert (await client.post("/equipment/AGV-01/release")).status_code == 200
        if outcome == "failed":
            assert core.status()["replans"] == 1


async def test_resolve_404_and_422():
    _, client, job_id = await _unknown_outcome()
    async with client:
        r = await client.post("/jobs/J-nope/resolve", json={"outcome": "completed"})
        assert r.status_code == 404
        assert r.json()["code"] == "UNKNOWN_JOB" and "J-nope" in r.json()["message"]
        r = await client.post(f"/jobs/{job_id}/resolve", json={"outcome": "maybe"})
        assert r.status_code == 422
        r = await client.post(f"/jobs/{job_id}/resolve", json={})
        assert r.status_code == 422
        r = await client.post(f"/jobs/{job_id}/resolve", json={"outcome": "completed"})
        assert r.status_code == 200
        r = await client.post(f"/jobs/{job_id}/resolve", json={"outcome": "completed"})
        assert r.status_code == 404  # no longer an unknown outcome


async def test_endpoints_503_without_connection():
    _, core, client = _setup(_fake(LosingPort))
    async with client:
        assert (await client.post("/equipment/AGV-01/release")).status_code == 503
        assert (await client.post("/jobs/J1/resolve", json={"outcome": "failed"})).status_code == 503
        await core.connect_once()
        core.on_link_lost()
        assert (await client.post("/equipment/AGV-01/release")).status_code == 503
        assert (await client.post("/jobs/J1/resolve", json={"outcome": "failed"})).status_code == 503
        assert (await client.get("/alarms")).status_code == 200  # last-known alarms stay readable


async def test_release_connection_error_drops_link_and_503():
    fake, core, client = _setup(_fake(LosingPort))
    async with client:
        await core.connect_once()
        fake.lost = True  # Dispatcher.release reads the device state
        r = await client.post("/equipment/AGV-01/release")
        assert r.status_code == 503 and core.connected is False


def test_ws_alarm_messages():
    fake = _fake()
    core = EcsCore(LAYOUT, connect=lambda: fake)
    with TestClient(create_app(core, run_loop=False)) as client:
        client.portal.call(core.connect_once)
        with client.websocket_connect("/events") as ws:
            assert ws.receive_json()["type"] == "status"

            def fault():
                fake.advance(3)
                fake.set_state("AGV-01", State.FAULT)
                core.step()

            def next_alarm() -> dict:
                while (msg := ws.receive_json())["type"] != "alarm":
                    pass
                return msg

            client.portal.call(fault)
            msg = next_alarm()
            assert msg == {"type": "alarm", "action": "raised", "kind": "NEEDS_RELEASE",
                           "equipment_id": "AGV-01", "job_id": None, "since_sim_time": 3.0,
                           "detail": msg["detail"], "sim_time": 3.0}
            fake.set_state("AGV-01", State.IDLE)
            assert client.post("/equipment/AGV-01/release").status_code == 200
            msg = next_alarm()
            assert (msg["action"], msg["kind"], msg["equipment_id"]) == ("cleared", "NEEDS_RELEASE", "AGV-01")


def test_openapi_declares_operator_endpoints():
    _, core, _ = _setup()
    spec = create_app(core, run_loop=False).openapi()
    rel = spec["paths"]["/equipment/{equipment_id}/release"]["post"]["responses"]
    refused = {"$ref": "#/components/schemas/RefusedOut"}
    assert rel["200"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ReleaseOut"}
    assert rel["404"]["content"]["application/json"]["schema"] == refused
    assert rel["409"]["content"]["application/json"]["schema"] == refused
    assert "503" in rel
    res = spec["paths"]["/jobs/{job_id}/resolve"]["post"]
    assert set(res["responses"]) >= {"200", "404", "422", "503"}
    assert res["responses"]["404"]["content"]["application/json"]["schema"] == refused
    assert spec["components"]["schemas"]["ResolveIn"]["properties"]["outcome"]["enum"] == ["completed", "failed"]
    assert "AlarmOut" in spec["components"]["schemas"]


def test_serve_auto_release_flag(monkeypatch):
    cores = []
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **kw: cores.append(app.state.core))
    monkeypatch.setattr(signal, "signal", lambda sig, h: None)
    monkeypatch.setattr(serve, "configure_json_logging", lambda level: None)
    base = ["--gateway", "opc.tcp://x", "--layout", str(ROOT / "terminal.yaml")]
    assert serve.main(base) == 0
    assert serve.main([*base, "--auto-release"]) == 0
    assert [c.auto_release for c in cores] == [False, True]


class CountdownPort(FakePort):
    """Answers ``ok_snapshots`` more snapshot reads once armed, then the link is lost."""

    ok_snapshots: int | None = None

    def snapshot(self, equipment_id):
        if self.ok_snapshots is not None:
            if self.ok_snapshots <= 0:
                raise ConnectionError("link lost")
            self.ok_snapshots -= 1
        return super().snapshot(equipment_id)

    def close(self) -> None:
        pass


async def test_release_succeeds_when_the_following_tick_loses_the_link():
    fake, core, client = _setup(_fake(CountdownPort))
    async with client:
        await core.connect_once()
        fake.set_state("AGV-01", State.FAULT)
        fake.set_state("AGV-01", State.IDLE)
        assert core.status()["needs_release"] == 1
        fake.ok_snapshots = 1  # the release check reads the state; the tick then fails
        r = await client.post("/equipment/AGV-01/release")
        assert r.status_code == 200 and r.json() == {"released": "AGV-01"}
        assert core.connected is False
        assert (await client.get("/health")).status_code == 503
        assert core.dispatcher.status()["needs_release"] == 0  # the release took effect
        # a retry while the link is down: 503, not a false 404
        assert (await client.post("/equipment/AGV-01/release")).status_code == 503


async def test_release_retry_after_reconnect():
    first, second = _fake(CountdownPort), _fake()
    ports = [first, second]
    core = EcsCore(LAYOUT, connect=lambda: ports.pop(0))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(core, run_loop=False)),
                               base_url="http://t")
    async with client:
        await core.connect_once()
        first.set_state("AGV-01", State.FAULT)
        first.ok_snapshots = 1
        r = await client.post("/equipment/AGV-01/release")
        assert r.status_code == 409  # refused before any change; the tick never ran
        assert core.connected is True
        first.ok_snapshots = None
        first.set_state("AGV-01", State.IDLE)
        first.ok_snapshots = 1
        assert (await client.post("/equipment/AGV-01/release")).status_code == 200
        assert core.connected is False
        assert await core.connect_once() is True
        r = await client.post("/equipment/AGV-01/release")  # retry: already released, still fine
        assert r.status_code == 200 and core.status()["needs_release"] == 0
        r = await client.post("/equipment/AGV-99/release")
        assert r.status_code == 404


async def test_resolve_succeeds_when_the_following_tick_loses_the_link():
    first, second, third = _fake(LosingPort), _fake(CountdownPort), _fake()
    ports = [first, second, third]
    core = EcsCore(LAYOUT, connect=lambda: ports.pop(0))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(core, run_loop=False)),
                               base_url="http://t")
    await core.connect_once()
    core.submit([WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")])
    job_id = first.calls[0][1].job_id
    core.on_link_lost()
    assert await core.connect_once() is True
    assert core.status()["unknown_outcomes"] == 1
    async with client:
        second.ok_snapshots = 0  # the verdict needs no port read; the tick does
        r = await client.post(f"/jobs/{job_id}/resolve", json={"outcome": "failed"})
        assert r.status_code == 200 and r.json() == {"job_id": job_id, "outcome": "failed"}
        assert core.connected is False
        assert (await client.get("/health")).status_code == 503
        assert core.dispatcher.unknown_jobs() == frozenset()
        # retry while the link is down: 503; after reconnecting: 404 (already resolved)
        assert (await client.post(f"/jobs/{job_id}/resolve", json={"outcome": "failed"})).status_code == 503
        assert await core.connect_once() is True
        r = await client.post(f"/jobs/{job_id}/resolve", json={"outcome": "failed"})
        assert r.status_code == 404 and r.json()["code"] == "UNKNOWN_JOB"


async def test_release_internal_keyerror_is_not_a_404(monkeypatch):
    _, core, client = _setup()
    async with client:
        await core.connect_once()

        def boom(*_a, **_k):
            raise KeyError("internal")

        monkeypatch.setattr(core.dispatcher, "release", boom)
        with pytest.raises(RuntimeError):
            await client.post("/equipment/AGV-01/release")
