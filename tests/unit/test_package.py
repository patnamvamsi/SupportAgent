"""WP-000: the package skeleton is importable and reports the version (10-wp L35)."""

from __future__ import annotations

import importlib

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
    """``import supportagent`` works, the version is 0.0.0, and every 01 §1 package imports."""
    supportagent = importlib.import_module("supportagent")
    assert supportagent.__version__ == "0.0.0"
    for name in _SUBPACKAGES:
        importlib.import_module(f"supportagent.{name}")
