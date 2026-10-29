"""``supportagent demo-servicenow``: the local install's demo ServiceNow (no real tickets).

The ``demo-servicenow`` Compose service runs it when ``./install.sh`` was told
to use the demo instance. It serves the Table API with an integration user and
a new sample incident every ``--interval-s`` seconds. Never point a production
config at it.
"""

from __future__ import annotations

import typer
import uvicorn

from supportagent.bootstrap import demo_servicenow_app


def demo_servicenow(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address (a container port)."),  # noqa: S104  # nosec B104
    port: int = typer.Option(8081, "--port", help="Port."),
    interval_s: float = typer.Option(300.0, "--interval-s", help="Seconds between incidents."),
    first_after_s: float = typer.Option(45.0, "--first-after-s", help="Before the first one."),
) -> None:
    """Serve the demo ServiceNow (local install only)."""
    app = demo_servicenow_app(interval_s=interval_s, first_after_s=first_after_s)
    uvicorn.run(app, host=host, port=port, log_level="warning", server_header=False)
