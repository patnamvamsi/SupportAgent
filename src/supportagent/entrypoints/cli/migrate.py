"""``supportagent migrate`` (02 §4): apply the schema to the target revision.

The database URL is never a CLI argument: it comes from the config file's
``database.url_env`` resolved against the environment (02 §1, 05 §6) — this
module only maps the outcome to the 02 §4 exit codes: 0 ok, 2 refused
(downgrade without ``--allow-downgrade``, or a config the operator must fix).
"""

from __future__ import annotations

from pathlib import Path

import typer

from supportagent.bootstrap import run_migrations
from supportagent.config.loader import load_config_default
from supportagent.domain.errors import (
    ConfigError,
    DomainValidationError,
    ExternalServiceError,
)


def migrate(
    # B008: typer.Option defaults are the Typer idiom; ruff 0.17 flags the
    # multiline form only, so the noqa is scoped to this line.
    config: Path = typer.Option(  # noqa: B008
        "supportagent.yaml",
        "--config",
        help="Path to supportagent.yaml (02 §1).",
    ),
    to: str = typer.Option("head", "--to", help="Target revision (02 §4: default head)."),
    allow_downgrade: bool = typer.Option(
        False,
        "--allow-downgrade",
        help="Permit downgrades (02 §4: refused without it, exit 2).",
    ),
) -> None:
    """Move the schema to ``--to`` (default head) on the configured database."""
    try:
        app_config = load_config_default(config)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    url = app_config.database.url.get_secret_value()
    try:
        run_migrations(url, to, allow_downgrade=allow_downgrade)
    except DomainValidationError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    except ExternalServiceError as exc:
        typer.echo(f"database error: {exc}", err=True)
        raise typer.Exit(1) from exc
