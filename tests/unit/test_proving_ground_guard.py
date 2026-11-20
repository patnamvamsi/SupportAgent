"""ADR-0022 §1: the core never names a proving ground.

A real system proves v0.2, but the product must discover it generically:
everything specific to it is the deployment's data. This test fails if any
name in ``tests/data/proving_ground_names.txt`` appears in a core file.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_CORE = ("src", "packs", "plugins", "prompts", "taxonomy")
_SKIP_PARTS = {"__pycache__", ".mypy_cache", ".ruff_cache"}


def _names() -> list[str]:
    text = (_REPO / "tests" / "data" / "proving_ground_names.txt").read_text("utf-8")
    return [
        line.strip().lower()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _core_files() -> list[Path]:
    return [
        path
        for root in _CORE
        for path in sorted((_REPO / root).rglob("*"))
        if path.is_file() and not _SKIP_PARTS.intersection(path.parts)
    ]


def test_core_names_no_target_system() -> None:
    names = _names()
    assert names, "the proving-ground name list is empty"
    found = []
    for path in _core_files():
        try:
            text = path.read_text("utf-8").lower()
        except UnicodeDecodeError:
            continue
        hits = [name for name in names if name in text or name in path.name.lower()]
        if hits:
            found.append(f"{path.relative_to(_REPO)}: {', '.join(hits)}")
    assert not found, "core files name a proving ground (ADR-0022 §1):\n" + "\n".join(found)


def test_guard_scans_the_core() -> None:
    files = _core_files()
    for root in _CORE:
        assert any(path.is_relative_to(_REPO / root) for path in files), root
