from __future__ import annotations

import csv
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from terminal_ecs.workorders import WorkOrder
from terminal_model.locations import parse_location
from terminal_model.types import JobType, State
from terminal_runner.run import main, prefill, run_once
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_sim.yard import Yard

LAYOUT = Path(__file__).resolve().parents[2] / "terminal.yaml"

# Half discharge, half load: exercises all four sim-side box handovers.
SMALL = """\
name: small
seed: 7
n_sts: 1
n_agv: 2
n_containers: 20
share_40ft: 0.5
discharge_share: 0.5
speed_factor: 1
duration_s: 7200
"""


@pytest.fixture
def small_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "small.yaml"
    path.write_text(SMALL)
    return path


def test_run_once_small(small_yaml: Path):
    scenario = load_scenario(small_yaml)
    k = run_once(scenario, load_layout(LAYOUT), seed=scenario.seed)
    assert k["containers_done"] == 20.0
    assert k["containers_failed"] == 0.0
    assert k["moves_per_hour_total"] > 0
    assert k["timed_out"] == 0.0
    assert 0 < k["sim_elapsed_s"] < scenario.duration_s
    assert list(k)[-2:] == ["sim_elapsed_s", "timed_out"]


def test_reproducible(small_yaml: Path):
    scenario = load_scenario(small_yaml)
    layout = load_layout(LAYOUT)
    assert run_once(scenario, layout, seed=3) == run_once(scenario, layout, seed=3)


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_cli_writes_csv_and_summary(small_yaml: Path, tmp_path: Path, capsys, restore_root_logging):
    out = tmp_path / "out"
    out.mkdir()
    (out / "small.csv").write_text("stale,header\n1,2\n")
    assert main([str(small_yaml), "--replications", "2", "--out", str(out), "--layout", str(LAYOUT)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == str(out / "small-summary.json")
    runs = [json.loads(line) for line in captured.err.splitlines() if '"terminal_runner.run"' in line]
    assert [(r["run_id"], r["seed"], r["timed_out"]) for r in runs] == [(0, 7, 0.0), (1, 8, 0.0)]
    with (out / "small.csv").open() as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 3  # header + 2 runs
    assert rows[0][0] == "run_id"
    summary = json.loads((out / "small-summary.json").read_text())
    stats = summary["moves_per_hour_total"]
    assert stats["p5"] <= stats["mean"] <= stats["p95"]
    assert stats["mean"] > 0


def test_duration_cap_is_never_overshot(small_yaml: Path):
    scenario = replace(load_scenario(small_yaml), duration_s=90.0)
    k = run_once(scenario, load_layout(LAYOUT), seed=scenario.seed)
    assert k["sim_elapsed_s"] == 90.0
    assert k["timed_out"] == 1.0
    assert k["containers_done"] < 20


def test_cli_rejects_zero_replications(small_yaml: Path, tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        main([str(small_yaml), "--replications", "0", "--out", str(tmp_path)])
    assert exc.value.code == 2
    assert "--replications" in capsys.readouterr().err


def test_run_sets_no_transfer_hook(small_yaml: Path, monkeypatch):
    # The sim hands boxes over itself; the ECS must not be given a hook.
    import terminal_runner.run as run_mod

    seen = []
    real = run_mod.Dispatcher

    def spy(*args, **kwargs):
        d = real(*args, **kwargs)
        seen.append(d)
        return d

    monkeypatch.setattr(run_mod, "Dispatcher", spy)
    scenario = replace(load_scenario(small_yaml), duration_s=60.0)
    run_once(scenario, load_layout(LAYOUT), seed=scenario.seed)
    assert len(seen) == 1 and seen[0].on_transfer is None


MIXED = """\
name: mixed
seed: 7  # stalled at 23/40 before AGV_TO_TP counted
n_sts: 2
n_agv: 6
n_containers: 40
share_40ft: 0.5
discharge_share: 0.5
speed_factor: 1
duration_s: 14400
"""


def test_mixed_load_discharge_with_many_agvs_completes(tmp_path: Path):
    # Regression: non-head LOAD AGVs fetching boxes from several TPs at once used to
    # fill both lane cells and lock the FIFO-head LOAD out (AGV_TO_TP not counted).
    path = tmp_path / "mixed.yaml"
    path.write_text(MIXED)
    scenario = load_scenario(path)
    k = run_once(scenario, load_layout(LAYOUT), seed=scenario.seed)
    assert k["containers_done"] == 40.0
    assert k["containers_failed"] == 0.0
    assert k["timed_out"] == 0.0


def test_prefill_puts_load_containers_in_yard():
    yard = Yard(load_layout(LAYOUT).blocks)
    orders = [
        WorkOrder("C0001", JobType.DISCHARGE, "STS-01", "A/1/1/1"),
        WorkOrder("C0002", JobType.LOAD, "STS-01", "A/1/1/2"),
        WorkOrder("C0003", JobType.LOAD, "STS-02", "B/2/3/1"),
    ]
    prefill(yard, orders)
    assert yard.locate("C0001") is None
    assert yard.locate("C0002") == parse_location("A/1/1/2")
    assert yard.locate("C0003") == parse_location("B/2/3/1")


def _dispatcher_spy(monkeypatch) -> list[dict]:
    import terminal_runner.run as run_mod

    kwargs_seen: list[dict] = []
    real = run_mod.Dispatcher

    def spy(*args, **kwargs):
        kwargs_seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_mod, "Dispatcher", spy)
    return kwargs_seen


def test_run_once_auto_release_default_true(small_yaml: Path, monkeypatch):
    seen = _dispatcher_spy(monkeypatch)
    scenario = replace(load_scenario(small_yaml), duration_s=60.0)
    run_once(scenario, load_layout(LAYOUT), seed=scenario.seed)
    run_once(scenario, load_layout(LAYOUT), seed=scenario.seed, auto_release=False)
    assert [k["auto_release"] for k in seen] == [True, False]


@pytest.mark.parametrize("flag,expected", [([], True), (["--no-auto-release"], False)])
def test_cli_no_auto_release_reaches_dispatcher(small_yaml: Path, tmp_path: Path, monkeypatch, flag, expected,
                                                restore_root_logging):
    seen = _dispatcher_spy(monkeypatch)
    small_short = tmp_path / "short.yaml"
    small_short.write_text(SMALL.replace("duration_s: 7200", "duration_s: 60"))
    assert main([str(small_short), "--out", str(tmp_path / "o"), "--layout", str(LAYOUT), *flag]) == 0
    assert [k["auto_release"] for k in seen] == [expected]


@pytest.mark.parametrize("flag,expected", [([], True), (["--no-auto-release"], False)])
def test_cli_no_auto_release_reaches_opcua_run(small_yaml: Path, monkeypatch, flag, expected):
    import terminal_runner.run as run_mod

    captured = []
    monkeypatch.setattr(run_mod, "run_replications", lambda *a, run, **kw: captured.append(run) or "x")
    monkeypatch.setattr(run_mod, "configure_json_logging", lambda: None)
    assert main([str(small_yaml), "--port", "opcua", *flag]) == 0
    (fn,) = captured
    assert fn.func is run_mod.run_once_opcua and fn.keywords["auto_release"] is expected


def test_cli_http_mode_ignores_auto_release(small_yaml: Path, monkeypatch):
    import terminal_runner.run as run_mod

    captured = []
    monkeypatch.setattr(run_mod, "run_replications", lambda *a, run, **kw: captured.append(run) or "x")
    monkeypatch.setattr(run_mod, "configure_json_logging", lambda: None)
    assert main([str(small_yaml), "--port", "http", "--no-auto-release"]) == 0
    (fn,) = captured
    assert fn.func is run_mod.run_once_http and "auto_release" not in fn.keywords


def test_help_documents_auto_release(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--no-auto-release" in out and "--port http" in out and "--auto-release" in out


class _StatePort:
    """Reports the queued states per poll (``equipment_ids`` starts each poll)."""

    def __init__(self, polls: list[dict[str, State]]) -> None:
        self.polls = polls
        self.current: dict[str, State] = {}
        self.n = 0

    def equipment_ids(self) -> list[str]:
        self.current = self.polls[min(self.n, len(self.polls) - 1)]
        self.n += 1
        return sorted(self.current)

    def snapshot(self, eid: str):
        return SimpleNamespace(state=self.current[eid])


def test_wait_ready_waits_out_offline(monkeypatch):
    import terminal_runner.run as run_mod

    sleeps = []
    monkeypatch.setattr(run_mod, "_sleep", sleeps.append)
    port = _StatePort([{"AGV-01": State.OFFLINE, "STS-01": State.IDLE},
                       {"AGV-01": State.FAULT, "STS-01": State.OFFLINE},
                       {"AGV-01": State.IDLE, "STS-01": State.IDLE}])
    run_mod._wait_ready(port)
    assert port.n == 3 and len(sleeps) == 2


def test_wait_ready_gives_up_on_offline_after_timeout(monkeypatch, caplog):
    import terminal_runner.run as run_mod

    t = [0.0]
    monkeypatch.setattr(run_mod, "_clock", lambda: t[0])
    monkeypatch.setattr(run_mod, "_sleep", lambda s: t.__setitem__(0, t[0] + s))
    port = _StatePort([{"AGV-01": State.OFFLINE}])
    with caplog.at_level(logging.WARNING):
        run_mod._wait_ready(port)
    assert t[0] >= run_mod.READY_TIMEOUT_S
    assert any("AGV-01" in r.getMessage() for r in caplog.records)
