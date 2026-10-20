"""``supportagent grants request --from config`` (02 §4, ARCH §6.4, 10-wp WP-012).

Writes ``requested`` policy grants from the config's ``grants_request`` list,
with patterns expanded via the service catalog into literal scopes. Requested
rows are not enforceable until an admin approves them (``/ui/grants``). Exit
codes follow ``migrate``: 0 ok, 2 config problem, 1 database problem.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import typer

from supportagent.bootstrap import request_grants_from_config
from supportagent.config.loader import load_config_default
from supportagent.domain.errors import ConfigError, ExternalServiceError

app = typer.Typer(help="Policy grant requests (ARCH §6.4).", no_args_is_help=True)


class Source(StrEnum):
    CONFIG = "config"


@app.command("request")
def request(
    source: Source = typer.Option(  # noqa: B008
        ..., "--from", help="Where the requests come from (02 §4: config)."
    ),
    config: Path = typer.Option(  # noqa: B008
        "supportagent.yaml", "--config", help="Path to supportagent.yaml (02 §1)."
    ),
) -> None:
    """Write ``requested`` grants from ``grants_request`` (patterns expanded via the catalog)."""
    del source  # one source today; the option keeps the 02 §4 invocation explicit
    try:
        app_config = load_config_default(config)
        created = request_grants_from_config(app_config)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except ExternalServiceError as exc:
        typer.echo(f"database error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"requested {len(created)} grant(s); approve them in /ui/grants")
