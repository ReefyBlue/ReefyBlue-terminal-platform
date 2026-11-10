from __future__ import annotations

import csv
import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest
from terminal_runner.run import _transfer_hook, main, run_once
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_sim.world import World

LAYOUT = Path(__file__).resolve().parents[2] / "terminal.yaml"

# Half discharge, half load: exercises all four on_transfer hooks.
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


def test_hook_rejects_a_non_agv_with_type_error(small_yaml: Path):
    world = World(load_layout(LAYOUT), load_scenario(small_yaml))
    with pytest.raises(TypeError, match="STS-01 is not an AGV"):
        _transfer_hook(world)("tp_to_agv", "STS-01", "C0001")
