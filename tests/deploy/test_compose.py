"""deploy/: compose renders, services/ports/health/restart as spec §2, soak scenario and env keys."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from terminal_model.types import Kind
from terminal_sim.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SERVICES = ["sim", "gateway", "chaos", "ecs", "soak"]
ENV_KEYS = {
    "SCENARIO": "soak.yaml", "SPEED_FACTOR": "1", "SOAK_HOURS": "72", "CUT_EVERY_S": "14400",
    "CUT_FOR_S": "30", "AUTO_RELEASE": "1", "LOW_WATER": "40", "BATCH": "60",
}


def _env_file(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def _config(env_file: Path) -> dict:
    proc = subprocess.run(
        ["docker", "compose", "-f", str(DEPLOY / "docker-compose.yml"), "--env-file", str(env_file),
         "config", "--format", "yaml"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    return yaml.safe_load(proc.stdout)


def _cmd(svc: dict) -> str:
    command = svc["command"]
    return command if isinstance(command, str) else " ".join(command)


needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")


def test_env_example_has_every_key():
    assert _env_file(DEPLOY / ".env.example") == ENV_KEYS


def test_soak_scenario_loads_with_faults_on_all_kinds():
    s = load_scenario(DEPLOY / "scenarios" / "soak.yaml")
    assert s.name == "soak"
    assert s.discharge_share == 0.5
    assert s.speed_factor == 1
    assert {k for k, fp in s.faults.items() if fp.mtbf_s > 0} == set(Kind)


def test_dockerignore_excludes_heavy_dirs():
    lines = {ln.strip().rstrip("/") for ln in (ROOT / ".dockerignore").read_text().splitlines()}
    assert {".git", ".venv", "out", "tests", "docs", ".superpowers"} <= lines


@needs_docker
def test_compose_renders_five_services():
    cfg = _config(DEPLOY / ".env.example")
    services = cfg["services"]
    assert sorted(services) == sorted(SERVICES)

    for name in SERVICES:
        svc = services[name]
        assert svc["logging"] == {"driver": "json-file", "options": {"max-size": "20m", "max-file": "5"}}
        if name == "soak":
            assert svc["restart"] == "on-failure"  # a normal exit ends the run; no fresh window
            assert "healthcheck" not in svc
        else:
            assert svc["restart"] == "unless-stopped"
            assert svc["healthcheck"]["test"]

    # only the ECS publishes a port, and only 8000:8000
    for name in SERVICES:
        if name != "ecs":
            assert not services[name].get("ports"), name
    (port,) = services["ecs"]["ports"]
    assert (str(port["published"]), port["target"]) == ("8000", 8000)

    chain = {"gateway": "sim", "chaos": "gateway", "ecs": "chaos", "soak": "ecs"}
    assert "depends_on" not in services["sim"]
    for name, dep in chain.items():
        deps = services[name]["depends_on"]
        assert list(deps) == [dep], name
        assert deps[dep]["condition"] == "service_healthy", name

    assert "/app/deploy/scenarios/soak.yaml" in _cmd(services["sim"])
    assert "--autostart" in _cmd(services["sim"])
    assert "--speed-factor 1" in _cmd(services["sim"])
    assert "--upstream-host sim" in _cmd(services["gateway"])
    assert "--target gateway:4840" in _cmd(services["chaos"])
    assert "--every 14400" in _cmd(services["chaos"]) and "--for 30" in _cmd(services["chaos"])
    assert "--gateway opc.tcp://chaos:4840" in _cmd(services["ecs"])
    soak = _cmd(services["soak"])
    assert "--ecs http://ecs:8000" in soak and "--hours 72" in soak and "--out /data/soak" in soak
    assert "--low-water 40" in soak and "--batch 60" in soak
    assert services["soak"]["stop_grace_period"] in ("1m0s", "60s", "1m")

    assert any(v["target"] == "/data/soak" and v["source"] == "soakdata" for v in services["soak"]["volumes"])
    assert "soakdata" in cfg["volumes"]


@needs_docker
@pytest.mark.parametrize("value, flag", [("1", True), ("0", False), ("", False)])
def test_auto_release_maps_to_flag(tmp_path, value, flag):
    env = dict(ENV_KEYS, AUTO_RELEASE=value)
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    ecs = _config(env_file)["services"]["ecs"]
    assert ecs["environment"]["AUTO_RELEASE"] == value
    # The flag is chosen by the container shell from AUTO_RELEASE; emulate that shell here.
    # `config` output re-escapes `$` as `$$`; the container receives single `$`.
    command = ecs["command"]
    assert command[:2] == ["sh", "-c"]
    script = command[2].replace("$$", "$").replace("exec python", "echo python")
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                         env={"AUTO_RELEASE": value, "PATH": "/usr/bin:/bin"}).stdout
    assert out.startswith("python -m terminal_ecs_service.serve")
    assert ("--auto-release" in out.split()) is flag


FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$*" in
  "compose ps -a -q chaos") ;;
  "compose ps -a -q "*) echo "id-$5" ;;
  "inspect "*) case "$4" in id-ecs) echo 2 ;; *) echo 0 ;; esac ;;
  *"sh -c cat > /data/soak/restarts.json") cat > "$FAKE_DIR/received-restarts.json" ;;
  *"cat /data/soak/samples.jsonl") printf '{"a": 1}\\n{"a": 2}\\n' ;;
  *"terminal_soak.report"*) echo "REPORT $*" ;;
esac
"""


@pytest.mark.parametrize("shell", ["sh", "bash"])  # bash: with pipefail active
@pytest.mark.parametrize("env_line, manual", [(None, False), ("AUTO_RELEASE=1", False),
                                              ("AUTO_RELEASE=0", True), ("AUTO_RELEASE=", True)])
def test_collect_sh(tmp_path, env_line, manual, shell):
    work = tmp_path / "deploy"
    work.mkdir()
    shutil.copy(DEPLOY / "collect.sh", work / "collect.sh")
    if env_line is not None:
        (work / ".env").write_text(f"SCENARIO=soak.yaml\n{env_line}\nBATCH=60\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_LOG=str(tmp_path / "log"),
               FAKE_DIR=str(tmp_path))
    proc = subprocess.run([shell, str(work / "collect.sh")], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "geen container voor chaos" in proc.stderr

    restarts = json.loads((work / "restarts.json").read_text())
    # no chaos container: its key is left out (report: "niet gemeten"), never -1
    assert restarts == {"sim": 0, "gateway": 0, "ecs": 2, "soak": 0}
    assert json.loads((tmp_path / "received-restarts.json").read_text()) == restarts
    assert (work / "samples.jsonl").read_text().splitlines() == ['{"a": 1}', '{"a": 2}']
    report = (work / "soak-report.md").read_text()
    assert "python -m terminal_soak.report /data/soak/samples.jsonl --restarts /data/soak/restarts.json" in report
    assert "run --rm --no-deps -T soak" in report
    assert ("--manual" in report) is manual


def test_collect_sh_fails_on_docker_error(tmp_path):
    """set -e(o pipefail): a failing docker call ends collect.sh non-zero."""
    work = tmp_path / "deploy"
    work.mkdir()
    shutil.copy(DEPLOY / "collect.sh", work / "collect.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("#!/bin/sh\nexit 3\n")
    (bin_dir / "docker").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    proc = subprocess.run(["sh", str(work / "collect.sh")], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode != 0
    assert not (work / "soak-report.md").exists()


def test_report_treats_missing_restart_key_as_not_measured():
    from terminal_soak.report import _c1
    c = _c1({"sim": 0, "gateway": 0, "ecs": 0, "soak": 0})
    assert c.passed is None and "chaos" in c.detail


def test_dockerfile_stages_are_bookworm_without_platform_pin():
    froms = [ln for ln in (DEPLOY / "Dockerfile").read_text().splitlines() if ln.startswith("FROM ")]
    assert len(froms) == 2
    assert all("bookworm" in ln and "--platform" not in ln for ln in froms)  # compose sets `platforms`
