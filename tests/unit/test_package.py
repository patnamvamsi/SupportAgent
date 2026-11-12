"""WP-000: the package skeleton is importable and reports the version (10-wp L35)."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

# Every package in the 01 §1 layout (``entrypoints/api/templates`` and
# ``static`` are asset directories, not Python packages).
_SUBPACKAGES: tuple[str, ...] = (
    "domain",
    "domain.model",
    "ports",
    "app",
    "app.doctor",
    "adapters",
    "adapters.db",
    "adapters.crypto",
    "adapters.system",
    "adapters.files",
    "adapters.llm",
    "adapters.llm.providers",
    "adapters.llm.detectors",
    "adapters.servicenow",
    "adapters.identity",
    "adapters.resilience",
    "adapters.tools",
    "adapters.cel",
    "adapters.paging",
    "adapters.webhook",
    "adapters.fakes",
    "config",
    "evals",
    "entrypoints",
    "entrypoints.api",
    "entrypoints.api.routes",
    "entrypoints.cli",
    "entrypoints.worker",
)


def test_package_importable() -> None:
    """``import supportagent`` works with pyproject's version; every 01 §1 package imports."""
    supportagent = importlib.import_module("supportagent")
    assert supportagent.__version__ == _pyproject_version()
    for name in _SUBPACKAGES:
        importlib.import_module(f"supportagent.{name}")


def _pyproject_version() -> str:
    """The version ``pyproject.toml`` declares: the one source of truth (06 §1)."""
    root = Path(__file__).resolve().parents[2]
    return str(tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"])
