"""WP-000: the CLI ``--version`` DoD (10-wp: ``supportagent --version`` prints the version).

The name keeps its WP-000 test ID; the version is ``pyproject.toml``'s, not a literal.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from supportagent.entrypoints.cli.main import app


def _pyproject_version() -> str:
    """The version ``pyproject.toml`` declares: the one source of truth (06 §1)."""
    root = Path(__file__).resolve().parents[2]
    return str(tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"])


def test_cli_version_prints_0_0_0() -> None:
    """``supportagent --version`` exits 0 and prints exactly the pyproject version."""
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == _pyproject_version()
