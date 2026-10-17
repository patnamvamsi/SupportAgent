"""WP-000: the CLI ``--version`` DoD (10-wp: ``supportagent --version`` prints ``0.0.0``)."""

from __future__ import annotations

from typer.testing import CliRunner

from supportagent.entrypoints.cli.main import app


def test_cli_version_prints_0_0_0() -> None:
    """``supportagent --version`` exits 0 and prints exactly ``0.0.0``."""
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "0.0.0"
