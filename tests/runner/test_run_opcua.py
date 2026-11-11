"""Runner OPC UA mode without a live gateway: connect retries and CLI guards."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from terminal_runner import run
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_gateway_unreachable_exits_1(monkeypatch, capsys):
    sleeps: list[float] = []
    monkeypatch.setattr(run, "_sleep", sleeps.append)
    endpoint = f"opc.tcp://127.0.0.1:{_free_port()}/nobody-home"
    scenario = load_scenario(ROOT / "scenarios/baseline.yaml")
    with pytest.raises(SystemExit) as exc:
        run.run_once_opcua(scenario, load_layout(ROOT / "terminal.yaml"), 1, gateway=endpoint)
    assert exc.value.code == 1
    assert endpoint in capsys.readouterr().err
    assert sleeps == [run.CONNECT_RETRY_S] * (run.CONNECT_ATTEMPTS - 1)
    assert (run.CONNECT_ATTEMPTS, run.CONNECT_RETRY_S) == (10, 2.0)


def test_cli_rejects_replications_in_opcua_mode(capsys):
    with pytest.raises(SystemExit) as exc:
        run.main([str(ROOT / "scenarios/baseline.yaml"), "--port", "opcua", "--replications", "2"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--replications" in err and "fresh sim process" in err


def test_cli_port_choices(capsys):
    with pytest.raises(SystemExit):
        run.main([str(ROOT / "scenarios/baseline.yaml"), "--port", "tcp"])
    assert "invalid choice" in capsys.readouterr().err


class FakeOpcPort:
    """The OPC UA port surface the runner uses, backed by an in-process World."""

    def __init__(self, world):
        self.world = world
        self.calls: list[str] = []
        self.started = False

    def equipment_ids(self):
        return self.world.port.equipment_ids()

    def snapshot(self, eid):
        return self.world.port.snapshot(eid)

    def assign_job(self, eid, job):
        return self.world.port.assign_job(eid, job)

    def cancel_job(self, eid):
        return self.world.port.cancel_job(eid)

    def reset(self, eid):
        return self.world.port.reset(eid)

    def subscribe(self, cb):
        self.world.port.subscribe(cb)

    def now(self):
        return self.world.now()

    def drain_events(self):
        return 0

    def prefill_yard(self, slots):
        self.calls.append("prefill")
        return True

    def start_sim(self):
        self.calls.append("start")
        self.started = True
        return True

    def stop_sim(self):
        self.calls.append("stop")
        return True

    def wait_for_sim_time(self, t, timeout_s):
        self.world.run(until=t)
        return True

    def close(self):
        self.calls.append("close")


def _fake(monkeypatch, scenario, **world_overrides):
    from dataclasses import replace

    from terminal_sim.world import make_world

    layout = load_layout(ROOT / "terminal.yaml")
    fake = FakeOpcPort(make_world(layout, replace(scenario, **world_overrides)))
    monkeypatch.setattr(run, "_connect", lambda gateway: fake)
    return fake, layout


def test_failure_after_start_stops_sim(monkeypatch):
    scenario = load_scenario(ROOT / "scenarios/baseline.yaml")
    fake, layout = _fake(monkeypatch, scenario)

    class Boom(run.Dispatcher):
        def tick(self):
            if fake.started:
                raise RuntimeError("boom")
            super().tick()

    monkeypatch.setattr(run, "Dispatcher", Boom)
    with pytest.raises(RuntimeError, match="boom"):
        run.run_once_opcua(scenario, layout, 1, gateway="opc.tcp://fake")
    assert fake.calls[-3:] == ["start", "stop", "close"]


def test_failure_before_start_stops_paused_sim(monkeypatch):
    from dataclasses import replace

    scenario = replace(load_scenario(ROOT / "scenarios/baseline.yaml"), discharge_share=0.5)
    fake, layout = _fake(monkeypatch, scenario)
    monkeypatch.setattr(fake, "prefill_yard", lambda slots: False)
    with pytest.raises(RuntimeError, match="PrefillYard rejected"):
        run.run_once_opcua(scenario, layout, 1, gateway="opc.tcp://fake")
    assert fake.calls == ["stop", "close"]


def test_stop_errors_on_failed_run_are_swallowed(monkeypatch, caplog):
    from dataclasses import replace

    scenario = replace(load_scenario(ROOT / "scenarios/baseline.yaml"), discharge_share=0.5)
    fake, layout = _fake(monkeypatch, scenario)
    monkeypatch.setattr(fake, "prefill_yard", lambda slots: False)

    def stop_raises():
        raise ConnectionError("gone")

    monkeypatch.setattr(fake, "stop_sim", stop_raises)
    with pytest.raises(RuntimeError, match="PrefillYard rejected"):  # the original error
        run.run_once_opcua(scenario, layout, 1, gateway="opc.tcp://fake")
    assert fake.calls == ["close"]
    assert any("Stop on opc.tcp://fake failed" in r.getMessage() for r in caplog.records)


def test_fleet_mismatch_exits_1(monkeypatch, capsys):
    scenario = load_scenario(ROOT / "scenarios/baseline.yaml")  # 2 STS, 6 AGV
    fake, layout = _fake(monkeypatch, scenario, n_agv=3)  # gateway started with --n-agv 3
    with pytest.raises(SystemExit) as exc:
        run.run_once_opcua(scenario, layout, 1, gateway="opc.tcp://fake")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--n-sts 2 --n-agv 6" in err and "AGV 3" in err
    assert "start" not in fake.calls and fake.calls[-2:] == ["stop", "close"]


def test_matching_fleet_passes_check(monkeypatch):
    scenario = load_scenario(ROOT / "scenarios/baseline.yaml")
    fake, layout = _fake(monkeypatch, scenario)
    run._check_fleet(fake, scenario, layout)  # no SystemExit


class _DrainFailsAfterReachingTime:
    """``wait_for_sim_time`` reaches ``t`` at once; the final drain finds the link dead."""

    def wait_for_sim_time(self, t, timeout_s):
        return True

    def drain_events(self):
        raise ConnectionError("link lost")

    def now(self):
        return 60.0


def test_advance_wraps_connection_error_of_the_final_drain():
    with pytest.raises(RuntimeError, match="sim time stalled: link lost") as exc:
        run._advance(_DrainFailsAfterReachingTime(), 60.0, 1.0)
    assert isinstance(exc.value.__cause__, ConnectionError)
