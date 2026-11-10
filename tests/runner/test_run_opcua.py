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
