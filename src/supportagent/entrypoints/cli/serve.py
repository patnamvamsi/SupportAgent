"""``supportagent serve`` (02 §4): run the API and admin UI with uvicorn.

The app is wired by ``bootstrap.build_api_context`` from the config file; the
OIDC client, database and master key are resolved there. Exit 2 on a config
problem, like ``migrate``.
"""

from __future__ import annotations

from pathlib import Path

import typer
import uvicorn

from supportagent.bootstrap import build_api_context
from supportagent.config.loader import load_config_default
from supportagent.domain.errors import ConfigError
from supportagent.entrypoints.api.app import create_app


def serve(
    config: Path = typer.Option(  # noqa: B008
        "supportagent.yaml", "--config", help="Path to supportagent.yaml (02 §1)."
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind."),
    port: int = typer.Option(8000, "--port", help="Port to bind."),
) -> None:
    """Serve the API and admin UI (``/healthz``, ``/auth``, ``/api/v1``, ``/ui``)."""
    try:
        app = create_app(build_api_context(load_config_default(config)))
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    uvicorn.run(app, host=host, port=port, proxy_headers=True, server_header=False)
