"""Analyst extensions (WP-M09): ``pack new|validate``, ``tool new``, ``scenario new``,
``runbook new`` (WP-M08 runbook distillation).

Every generator writes a scaffold that already validates and never
overwrites a file. ``pack validate --strict`` is the PR check: it also fails
while generator text ("Replace me", "FILL IN") is left. A request for a
write or a new action kind is refused (exit 2) and routed to engineering.
The config is read offline: no command here reaches an external system.
Exit 0 ok, 1 validation problems, 2 usage, config error or refusal.
"""

from __future__ import annotations

from pathlib import Path

import typer

from supportagent.app.extend import Scaffold, pack_scaffold, tool_scaffold
from supportagent.bootstrap import (
    KNOWN_TOOL_SCHEMAS,
    distill_runbook,
    incident_scenario,
    validate_pack,
    write_scaffold,
)
from supportagent.config.loader import load_config_offline
from supportagent.domain.errors import ConfigError

_CONFIG = typer.Option(Path("supportagent.example.yaml"), "--config", help="Config file.")
_ROOT = typer.Option(Path(), "--root", help="Repository root to write under.")
_PACK_DIR = typer.Argument(..., help="The pack directory (packs/<id>).")

pack_app = typer.Typer(help="Skill packs (ADR-0011 §1).", no_args_is_help=True)
tool_app = typer.Typer(help="Tool capabilities (ADR-0011 §2).", no_args_is_help=True)
scenario_app = typer.Typer(help="Incident-lab scenarios (ADR-0011 §5).", no_args_is_help=True)
runbook_app = typer.Typer(help="Runbooks distilled from team docs (WP-M08).", no_args_is_help=True)


def _write(scaffold: Scaffold, root: Path) -> None:
    for path in write_scaffold(scaffold, root):
        typer.echo(f"wrote {path}")
    typer.echo("next:")
    for step in scaffold.next_steps:
        typer.echo(f"  - {step}")


@pack_app.command("new")
def pack_new(
    pack_id: str = typer.Argument(..., help="The pack id (its directory name)."),
    stack: str = typer.Option(..., "--stack", help="Comma-separated catalog stack tags."),
    description: str = typer.Option(..., "--description", help="One line: what it covers."),
    root: Path = _ROOT,
) -> None:
    """Scaffold ``packs/<id>/``: one example of every part, plus a scenario and a control."""
    try:
        _write(pack_scaffold(pack_id, stack=stack.split(","), description=description), root)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


@pack_app.command("validate")
def pack_validate(
    path: Path = _PACK_DIR,
    strict: bool = typer.Option(False, "--strict", help="Fail on unfinished generator text."),
    config: Path = _CONFIG,
) -> None:
    """Load, compile and cover-check the pack and its scenarios, as the lab would."""
    try:
        result = validate_pack(load_config_offline(config), path, strict=strict)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    for note in result.notes:
        typer.echo(f"note: {note}")
    for problem in result.problems:
        typer.echo(f"problem: {problem}")
    typer.echo(f"pack {result.pack_id}: {'ok' if result.ok else 'not ok'}")
    if not result.ok:
        raise typer.Exit(1)


@tool_app.command("new")
def tool_new(  # noqa: PLR0913, PLR0917 — one option per choice
    capability: str = typer.Argument(..., help="<area>.<verb>, e.g. queue.depth"),
    mcp: str | None = typer.Option(None, "--mcp", help="Bind to this registered MCP server."),
    mcp_tool: str | None = typer.Option(None, "--mcp-tool", help="The server's tool name."),
    trust_class: str = typer.Option("read", "--trust-class", help="Only read is accepted."),
    tier: str = typer.Option("T0", "--tier", help="Only T0 is accepted."),
    root: Path = _ROOT,
) -> None:
    """A read-only capability: adapter, fake and contract test; or an MCP binding snippet."""
    try:
        scaffold = tool_scaffold(
            capability,
            known=KNOWN_TOOL_SCHEMAS,
            trust_class=trust_class,
            tier=tier,
            mcp_server=mcp,
            mcp_tool=mcp_tool,
        )
        _write(scaffold, root)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


@scenario_app.command("new")
def scenario_new(  # noqa: PLR0913, PLR0917 — one option per choice
    pack: str = typer.Option(..., "--pack", help="The pack the scenario tests."),
    from_incident: Path = typer.Option(  # noqa: B008
        ..., "--from-incident", help="An incident record (JSON or YAML, e.g. a Table API row)."
    ),
    service: str | None = typer.Option(None, "--service", help="Catalog service to copy."),
    scrub: list[str] = typer.Option(  # noqa: B008
        [], "--scrub", help="A term to redact (customer, host); repeatable."
    ),
    split: str = typer.Option("holdout", "--split", help="holdout (default) | tune"),
    config: Path = _CONFIG,
    root: Path = _ROOT,
) -> None:
    """A redacted scenario with a ground-truth form; it loads once the form is filled in."""
    try:
        scaffold = incident_scenario(
            load_config_offline(config),
            pack,
            from_incident,
            scrub=scrub,
            service=service,
            split=split,
        )
        _write(scaffold, root)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


@runbook_app.command("new")
def runbook_new(  # noqa: PLR0913, PLR0917 — one option per choice
    path: Path = _PACK_DIR,
    doc: Path = typer.Option(..., "--doc", help="A Git team doc with `hypotheses:` front matter."),  # noqa: B008
    classification: str = typer.Option(
        ..., "--classification", help="The pack label it applies to."
    ),
    runbook_id: str | None = typer.Option(
        None, "--id", help="Runbook id (default: the doc's name)."
    ),
    config: Path = _CONFIG,
    root: Path = _ROOT,
) -> None:
    """A draft runbook distilled from a team doc: its hypotheses, in order, as the pack's probes."""
    try:
        scaffold = distill_runbook(
            load_config_offline(config), path, doc, classification=classification, slug=runbook_id
        )
        _write(scaffold, root)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


__all__ = ["pack_app", "runbook_app", "scenario_app", "tool_app"]
