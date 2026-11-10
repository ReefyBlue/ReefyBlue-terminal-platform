"""Runner ``--port http`` against a scripted ECS service (``httpx.MockTransport``) and a
fake gateway port: order of operations, KPIs, health/fleet guards, failure Stop, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from terminal_runner import run
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_tos_stub.client import EcsClient, WorkOrderRejected

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = load_scenario(ROOT / "scenarios/baseline.yaml")  # 2 STS, 6 AGV, 200 DISCHARGE
LAYOUT = load_layout(ROOT / "terminal.yaml")
KPIS = {"moves_per_hour_total": 60.0, "containers_done": 200.0, "sim_elapsed_s": 999.0}


def _fleet(n_sts=2, n_agv=6, n_asc=None) -> list[dict]:
    n_asc = len(LAYOUT.blocks) if n_asc is None else n_asc
    out = []
    for kind, n in (("STS", n_sts), ("AGV", n_agv), ("ASC", n_asc)):
        out += [{"equipment_id": f"{kind}-{i:02d}", "kind": kind} for i in range(1, n + 1)]
    return out


class ScriptedEcs:
    """The ECS service API: ``statuses`` is a list of ``(open, sim_time)`` served in turn
    (the last one repeats)."""

    def __init__(self, statuses, *, healthy=True, fleet=None, post_status=202):
        self.statuses = list(statuses)
        self.healthy = healthy
        self.fleet = fleet if fleet is not None else _fleet()
        self.post_status = post_status
        self.calls: list[str] = []
        self.posted: list[dict] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == "/health":
            if not self.healthy:
                return httpx.Response(503, json={"connected": False, "sim_time": None})
            return httpx.Response(200, json={"connected": True, "sim_time": 0.0})
        if path == "/equipment":
            return httpx.Response(200, json=self.fleet)
        if path == "/workorders":
            self.posted = json.loads(request.content)
            if self.post_status == 422:
                return httpx.Response(422, json={"errors": [{"container_id": None, "code": "X", "message": "x"}]})
            return httpx.Response(202, json={"accepted": len(self.posted)})
        if path == "/status":
            open_, sim_time = self.statuses[0] if len(self.statuses) == 1 else self.statuses.pop(0)
            return httpx.Response(200, json={"open": open_, "sim_time": sim_time})
        if path == "/kpis":
            return httpx.Response(200, json=dict(KPIS))
        return httpx.Response(404)


class FakeGatewayPort:
    def __init__(self, calls: list[str]):
        self.calls = calls
        self.prefill_ok = True

    def prefill_yard(self, slots):
        self.calls.append("prefill")
        return self.prefill_ok

    def start_sim(self):
        self.calls.append("start")
        return True

    def stop_sim(self):
        self.calls.append("stop")
        return True

    def drain_events(self):
        return 0

    def close(self):
        self.calls.append("close")


@pytest.fixture
def setup(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(run, "_sleep", sleeps.append)
    log: list[str] = []
    port = FakeGatewayPort(log)
    gateways: list[str] = []

    def connect(gateway):
        gateways.append(gateway)
        log.append("connect")
        return port

    monkeypatch.setattr(run, "_connect", connect)

    def make(ecs: ScriptedEcs) -> EcsClient:
        def handler(request):
            log.append(f"{request.method} {request.url.path}")
            return ecs(request)

        return EcsClient("http://ecs", transport=httpx.MockTransport(handler))

    return port, log, sleeps, gateways, make


def _run(client, scenario=SCENARIO):
    return run.run_once_http(scenario, LAYOUT, 1, ecs="http://ecs", gateway="opc.tcp://gw", client=client)


def test_run_once_http_happy_path(setup):
    _, log, sleeps, gateways, make = setup
    ecs = ScriptedEcs([(200, 0.0), (3, 60.0), (3, 120.0), (0, 180.0)])
    k = _run(make(ecs))
    assert k == {**KPIS, "sim_elapsed_s": 180.0, "timed_out": 0.0}
    assert list(k)[-2:] == ["sim_elapsed_s", "timed_out"]
    assert gateways == ["opc.tcp://gw"]
    assert len(ecs.posted) == SCENARIO.n_containers
    assert ecs.posted[0].keys() == {"container_id", "type", "sts_id", "yard_slot"}
    # health -> fleet -> connect -> (no LOAD: no prefill) -> t0 -> POST -> Start -> poll -> kpis -> Stop
    assert log == ["GET /health", "GET /equipment", "connect", "GET /status", "POST /workorders",
                   "start", "GET /status", "GET /status", "GET /status", "GET /kpis", "stop", "close"]
    assert sleeps == [run.STATUS_POLL_S] * 2
    assert (run.STATUS_POLL_S, run.HEALTH_TIMEOUT_S, run.STALL_TIMEOUT_S) == (0.5, 30.0, 120.0)


def test_run_once_http_prefills_load_containers(setup):
    from dataclasses import replace

    _, log, _, _, make = setup
    k = _run(make(ScriptedEcs([(0, 0.0)])), scenario=replace(SCENARIO, discharge_share=0.5))
    assert k["timed_out"] == 0.0
    assert log.index("prefill") < log.index("POST /workorders") < log.index("start")


def test_prefill_rejected_stops_paused_sim(setup):
    from dataclasses import replace

    port, log, _, _, make = setup
    port.prefill_ok = False
    with pytest.raises(RuntimeError, match="PrefillYard rejected"):
        _run(make(ScriptedEcs([(0, 0.0)])), scenario=replace(SCENARIO, discharge_share=0.5))
    assert "POST /workorders" not in log and log[-2:] == ["stop", "close"]


def test_timed_out_at_duration_cap(setup):
    from dataclasses import replace

    _, log, _, _, make = setup
    scenario = replace(SCENARIO, duration_s=120.0)
    k = _run(make(ScriptedEcs([(200, 10.0), (5, 70.0), (5, 130.0)])), scenario=scenario)
    assert k["timed_out"] == 1.0 and k["sim_elapsed_s"] == 120.0  # from t0 = 10
    assert log[-2:] == ["stop", "close"]


def test_sim_time_stall_raises_and_stops(setup, monkeypatch):
    _, log, sleeps, _, make = setup
    clock = [0.0]
    monkeypatch.setattr(run, "_clock", lambda: clock[0])
    monkeypatch.setattr(run, "_sleep", lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)))
    with pytest.raises(RuntimeError, match="sim time stalled"):
        _run(make(ScriptedEcs([(200, 0.0), (5, 60.0)])))
    # sim time moved to 60 at the first poll, then stood still for 120 s wall
    assert len(sleeps) == run.STALL_TIMEOUT_S / run.STATUS_POLL_S
    assert "GET /kpis" not in log and log[-2:] == ["stop", "close"]


def test_rejected_workorders_stop_sim(setup):
    _, log, _, _, make = setup
    with pytest.raises(WorkOrderRejected):
        _run(make(ScriptedEcs([(0, 0.0)], post_status=422)))
    assert "start" not in log and log[-2:] == ["stop", "close"]


def test_unhealthy_ecs_exits_1(setup, monkeypatch, capsys):
    _, log, _, gateways, make = setup
    waited: list[float] = []
    client = make(ScriptedEcs([(0, 0.0)], healthy=False))
    real = client.wait_healthy
    monkeypatch.setattr(client, "wait_healthy", lambda t, **kw: (waited.append(t), real(0.0))[1])
    with pytest.raises(SystemExit) as exc:
        _run(client)
    assert exc.value.code == 1
    assert waited == [30.0]
    assert "http://ecs" in capsys.readouterr().err
    assert gateways == [] and log == ["GET /health"]


def test_fleet_mismatch_exits_1(setup, capsys):
    _, log, _, gateways, make = setup
    with pytest.raises(SystemExit) as exc:
        _run(make(ScriptedEcs([(0, 0.0)], fleet=_fleet(n_agv=3))))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--n-sts 2 --n-agv 6" in err and "AGV 3" in err
    assert gateways == [] and log == ["GET /health", "GET /equipment"]


def test_fleet_mismatch_message_shared_with_opcua():
    from terminal_model.types import Kind

    ok = {Kind.STS: 2, Kind.AGV: 6, Kind.ASC: len(LAYOUT.blocks)}
    assert run._fleet_mismatch(ok, SCENARIO, LAYOUT) is None
    msg = run._fleet_mismatch({**ok, Kind.AGV: 3}, SCENARIO, LAYOUT)
    assert msg == (f"gateway fleet (STS 2, AGV 3, ASC {len(LAYOUT.blocks)}) does not match the scenario "
                   f"(STS 2, AGV 6, ASC {len(LAYOUT.blocks)}): start the gateway with --n-sts 2 --n-agv 6")


def test_own_client_is_closed(setup, monkeypatch):
    _, _, _, _, make = setup
    client = make(ScriptedEcs([(0, 0.0)]))
    closed: list[bool] = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    monkeypatch.setattr(run, "EcsClient", lambda base_url: client)
    run.run_once_http(SCENARIO, LAYOUT, 1, ecs="http://ecs", gateway="opc.tcp://gw")
    assert closed == [True]


def test_passed_client_is_not_closed(setup, monkeypatch):
    _, _, _, _, make = setup
    client = make(ScriptedEcs([(0, 0.0)]))
    closed: list[bool] = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    _run(client)
    assert closed == []


def test_cli_rejects_replications_in_http_mode(capsys):
    with pytest.raises(SystemExit) as exc:
        run.main([str(ROOT / "scenarios/baseline.yaml"), "--port", "http", "--replications", "2"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--replications" in err and "--port http" in err and "fresh sim process" in err


def test_cli_http_mode_wires_ecs_and_gateway(monkeypatch, tmp_path):
    seen = {}

    def fake_replications(scenario_path, replications, out_dir, layout_path, run):
        seen["run"] = run
        return tmp_path / "summary.json"

    monkeypatch.setattr(run, "run_replications", fake_replications)
    monkeypatch.setattr(run, "configure_json_logging", lambda: None)
    assert run.main([str(ROOT / "scenarios/baseline.yaml"), "--port", "http",
                     "--ecs", "http://ecs:9000", "--gateway", "opc.tcp://gw:1"]) == 0
    fn = seen["run"]
    assert fn.func is run.run_once_http and fn.keywords == {"ecs": "http://ecs:9000", "gateway": "opc.tcp://gw:1"}


def test_cli_ecs_default():
    import argparse

    captured = {}
    orig = argparse.ArgumentParser.parse_args

    def spy(self, argv=None, namespace=None):
        ns = orig(self, argv, namespace)
        captured["ecs"] = ns.ecs
        raise SystemExit(0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(argparse.ArgumentParser, "parse_args", spy)
        with pytest.raises(SystemExit):
            run.main([str(ROOT / "scenarios/baseline.yaml")])
    assert captured["ecs"] == "http://localhost:8000"
