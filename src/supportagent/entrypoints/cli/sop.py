"""``supportagent sop list`` / ``sop auto`` (ADR-0014 part 2, BL-054).

``sop list`` prints every SOP step in the configured knowledge sources with
the literal scope an auto grant names. ``sop auto <scope>`` writes a
``requested`` auto-mode grant with its conditions. Nothing runs on auto until
an admin approves the grant in ``/ui/grants``, and revoking it there stops
auto mode at once. Exit codes follow ``grants request``: 0 ok, 2 config or
input problem, 1 database problem.
"""

from __future__ import annotations

from pathlib import Path

import typer

from supportagent.bootstrap import list_sop_steps, request_sop_auto
from supportagent.config.loader import load_config_default
from supportagent.domain.auto import AutoConditions, reason
from supportagent.domain.errors import (
    ConcurrencyConflict,
    ConfigError,
    DomainValidationError,
    ExternalServiceError,
)

app = typer.Typer(help="SOP steps and their auto mode (ADR-0014).", no_args_is_help=True)

_CONFIG = typer.Option("supportagent.yaml", "--config", help="Path to supportagent.yaml (02 §1).")


@app.command("list")
def list_steps(config: Path = _CONFIG) -> None:
    """Every SOP step, with the scope ``sop auto`` takes."""
    try:
        steps = list_sop_steps(load_config_default(config))
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not steps:
        typer.echo("no SOPs in the configured knowledge sources")
        return
    for step in steps:
        stale = " (stale: refused for auto mode)" if step.stale else ""
        version = f" v{step.version}" if step.version else ""
        typer.echo(f"{step.scope}{stale}")
        typer.echo(f"  {step.title}{version}, when {', '.join(step.when)}: {step.note}")


@app.command("auto")
def auto(
    scope: str = typer.Argument(..., help="The step's scope, from `sop list`."),
    check: list[str] = typer.Option(  # noqa: B008
        ..., "--check", help="A hypothesis whose tool checks must hold (repeatable)."
    ),
    max_per_day: int = typer.Option(..., "--max-per-day", help="Daily cap on auto actions."),
    service: list[str] = typer.Option(  # noqa: B008
        [], "--service", help="Limit to these services (repeatable; default: any)."
    ),
    config: Path = _CONFIG,
) -> None:
    """Request auto mode for one SOP step; an admin approves it in /ui/grants."""
    try:
        conditions = AutoConditions(
            checks=tuple(check), max_per_day=max_per_day, services=tuple(service)
        )
        grant_id = request_sop_auto(load_config_default(config), scope, conditions)
    except DomainValidationError as exc:
        typer.echo(f"refused: {reason(exc)}", err=True)
        raise typer.Exit(2) from exc
    except (ConfigError, ConcurrencyConflict) as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    except ExternalServiceError as exc:
        typer.echo(f"database error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"requested {grant_id}; approve it in /ui/grants (its conditions are shown)")
