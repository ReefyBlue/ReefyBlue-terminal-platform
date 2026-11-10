"""Import rules between workspace packages, checked on import/from lines only."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imports(package_dir: str, forbidden: list[str]) -> list[str]:
    pattern = re.compile(rf"^\s*(?:from|import)\s+(?:{'|'.join(forbidden)})\b", re.MULTILINE)
    hits = []
    for path in (ROOT / "packages" / package_dir / "src").rglob("*.py"):
        for m in pattern.finditer(path.read_text()):
            hits.append(f"{path.relative_to(ROOT)}: {m.group().strip()}")
    return hits


def test_ecs_never_imports_sim():
    assert _imports("ecs", ["terminal_sim"]) == []


def test_sim_never_imports_ecs_tos_or_runner():
    assert _imports("sim", ["terminal_ecs", "terminal_tos_stub", "terminal_runner"]) == []


def test_model_imports_no_other_terminal_package():
    assert _imports("model", ["terminal_sim", "terminal_ecs", "terminal_tos_stub", "terminal_runner"]) == []


def test_guard_detects_a_real_import_but_not_a_docstring(tmp_path, monkeypatch):
    pkg = tmp_path / "packages" / "ecs" / "src"
    pkg.mkdir(parents=True)
    (pkg / "doc.py").write_text('"""Mentions terminal_sim in prose."""\n')
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert _imports("ecs", ["terminal_sim"]) == []
    (pkg / "bad.py").write_text("from terminal_sim.world import World\n")
    assert len(_imports("ecs", ["terminal_sim"])) == 1
