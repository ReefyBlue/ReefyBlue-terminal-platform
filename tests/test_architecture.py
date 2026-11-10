"""Import rules between workspace packages, checked on import/from lines only,
plus a guard that ``terminal_ecs`` is byte-identical to Plan A (``master``)."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ["model", "sim", "ecs", "tos_stub", "runner", "gateway", "opcua", "kpi"]
ECS_HASH_FIXTURE = ROOT / "tests" / "fixtures" / "ecs_sources.sha256"

# Workspace packages each package may import (besides itself); runner may import all.
ALLOWED = {
    "model": set(),
    "opcua": {"terminal_model"},
    "ecs": {"terminal_model"},
    "sim": {"terminal_model", "terminal_opcua"},
    "gateway": {"terminal_model", "terminal_opcua"},
    "tos_stub": {"terminal_model", "terminal_ecs"},
    "kpi": {"terminal_model", "terminal_ecs"},
}
_TERMINAL = re.compile(r"^\s*(?:from|import)\s+(terminal_\w+)", re.MULTILINE)


def _sources(package_dir: str) -> list[Path]:
    return sorted((ROOT / "packages" / package_dir / "src").rglob("*.py"))


def _imports(package_dir: str, forbidden: list[str]) -> list[str]:
    pattern = re.compile(rf"^\s*(?:from|import)\s+(?:{'|'.join(forbidden)})\b", re.MULTILINE)
    hits = []
    for path in _sources(package_dir):
        for m in pattern.finditer(path.read_text()):
            hits.append(f"{path.relative_to(ROOT)}: {m.group().strip()}")
    return hits


@pytest.mark.parametrize("package_dir", PACKAGES)
def test_every_scanned_package_has_sources(package_dir):
    # Guards against the rules below passing vacuously on a moved/empty package.
    assert len(_sources(package_dir)) >= 1


def _terminal_imports(package_dir: str) -> set[str]:
    own = f"terminal_{package_dir}"
    return {m.group(1) for p in _sources(package_dir) for m in _TERMINAL.finditer(p.read_text())} - {own}


@pytest.mark.parametrize("package_dir", sorted(ALLOWED))
def test_workspace_imports_within_allow_list(package_dir):
    assert _terminal_imports(package_dir) <= ALLOWED[package_dir]


def test_opcua_imports_only_model_and_asyncua():
    allowed = {"terminal_model", "terminal_opcua", "asyncua", "__future__"}
    top = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", re.MULTILINE)
    found = {m.group(1) for p in _sources("opcua") for m in top.finditer(p.read_text())}
    assert found - allowed - set(sys.stdlib_module_names) == set()


def test_sim_and_gateway_use_opcua():
    assert "terminal_opcua" in _terminal_imports("sim")
    assert "terminal_opcua" in _terminal_imports("gateway")


def _ecs_sources_sha256(root: Path) -> str:
    """sha256 over ``packages/ecs/src/**/*.py`` sorted by repo-relative path, each file
    hashed as ``path NUL content NUL`` (same recipe as the fixture taken on master)."""
    h = hashlib.sha256()
    src = root / "packages" / "ecs" / "src"
    for rel in sorted(p.relative_to(root).as_posix() for p in src.rglob("*.py")):
        h.update(rel.encode() + b"\0")
        h.update((root / rel).read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def test_ecs_unchanged_since_plan_a():
    assert _ecs_sources_sha256(ROOT) == ECS_HASH_FIXTURE.read_text().strip()


def test_ecs_hash_detects_a_change(tmp_path):
    src = tmp_path / "packages" / "ecs" / "src" / "terminal_ecs"
    src.mkdir(parents=True)
    (src / "a.py").write_text("x = 1\n")
    before = _ecs_sources_sha256(tmp_path)
    (src / "a.py").write_text("x = 2\n")
    assert _ecs_sources_sha256(tmp_path) != before


def test_ecs_never_imports_sim():
    assert _imports("ecs", ["terminal_sim"]) == []


def test_ecs_imports_only_model():
    assert _imports("ecs", ["terminal_sim", "terminal_tos_stub", "terminal_runner"]) == []


def test_sim_never_imports_ecs_tos_or_runner():
    assert _imports("sim", ["terminal_ecs", "terminal_tos_stub", "terminal_runner"]) == []


def test_model_imports_no_other_terminal_package():
    assert _imports("model", ["terminal_sim", "terminal_ecs", "terminal_tos_stub", "terminal_runner"]) == []


def test_tos_stub_imports_neither_sim_nor_runner():
    assert _imports("tos_stub", ["terminal_sim", "terminal_runner"]) == []


def test_runner_may_import_all_packages():
    assert _imports("runner", ["terminal_model", "terminal_sim", "terminal_ecs", "terminal_tos_stub"])


def test_gateway_imports_only_model_and_opcua():
    assert _imports("gateway", ["terminal_sim", "terminal_ecs", "terminal_tos_stub", "terminal_runner"]) == []
    allowed = {"terminal_model", "terminal_opcua", "terminal_gateway", "asyncua", "yaml", "__future__"}
    top = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", re.MULTILINE)
    found = {m.group(1) for p in _sources("gateway") for m in top.finditer(p.read_text())}
    assert found - allowed - set(sys.stdlib_module_names) == set()
    assert "terminal_opcua" in found


def test_guard_detects_a_real_import_but_not_a_docstring(tmp_path, monkeypatch):
    pkg = tmp_path / "packages" / "ecs" / "src"
    pkg.mkdir(parents=True)
    (pkg / "doc.py").write_text('"""Mentions terminal_sim in prose."""\n')
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert _imports("ecs", ["terminal_sim"]) == []
    (pkg / "bad.py").write_text("from terminal_sim.world import World\n")
    assert len(_imports("ecs", ["terminal_sim"])) == 1
