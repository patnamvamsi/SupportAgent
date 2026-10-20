"""``supportagent shadow import <export.jsonl>`` (02 §4, 10-wp WP-014).

Replays a partner's ServiceNow history export (format: ``adapters/servicenow/
export.py``) through the shadow runner, in ``opened_at`` order with the clock at
each record's ``opened_at``, then records the human labels. Nothing is posted
to ServiceNow. Exit 2 on a config or export problem, 1 on a database problem.
"""

from __future__ import annotations

from pathlib import Path

import typer

from supportagent.bootstrap import shadow_import
from supportagent.config.loader import load_config_default
from supportagent.domain.errors import ConfigError, ExternalServiceError

app = typer.Typer(help="Shadow mode (ARCH §8.4).", no_args_is_help=True)


@app.command("import")
def import_(
    export: Path = typer.Argument(help="Partner export, one ServiceNow record per line."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        "supportagent.yaml", "--config", help="Path to supportagent.yaml (02 §1)."
    ),
) -> None:
    """Replay historical tickets through the shadow runner."""
    try:
        records = shadow_import(load_config_default(config), export)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except ExternalServiceError as exc:
        typer.echo(f"database error: {exc}", err=True)
        raise typer.Exit(1) from exc
    labelled = [r for r in records if r.agreement is not None]
    agreed = sum(1 for r in labelled if r.agreement)
    typer.echo(
        f"replayed {len(records)} incident(s); {agreed}/{len(labelled)} labelled agree. "
        "See `supportagent stats --mode shadow --window all`."
    )
