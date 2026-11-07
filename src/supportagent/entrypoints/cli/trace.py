"""``supportagent trace`` / ``flow list`` / ``flow validate`` (ADR-0021).

``trace FLOW ID`` looks one business object up through a reviewed flow and
prints the timeline and the verdict: where it is, and whether that is where it
should be. With ``--explain`` (the default) RCA runs where it stopped and the
model writes a cited answer; every sentence is checked against its facts.
Every lookup is an audited tool call, and the trace itself is one ``trace``
event. Read-only: it never routes, claims or proposes.

Exit codes: 0 ok, 2 config or input problem, 1 database problem.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import typer

from supportagent.app.trace_answer import TraceAnswer
from supportagent.app.tracing import TraceRun
from supportagent.bootstrap import load_flow_file, load_flows, trace_object
from supportagent.config.loader import load_config_default
from supportagent.domain.errors import ConfigError, DomainValidationError, ExternalServiceError

flow_app = typer.Typer(
    help="Business flows for transaction tracing (ADR-0021).", no_args_is_help=True
)

_CONFIG = typer.Option("supportagent.yaml", "--config", help="Path to supportagent.yaml (02 §1).")
_MARK = {"found": "✓", "missing": "·", "unknown": "?"}


def trace(  # noqa: PLR0913, PLR0917 — one option per CLI flag
    flow: str = typer.Argument(..., help="The flow id (`supportagent flow list`)."),
    object_id: str = typer.Argument(..., help="The object to trace (a trade, an order)."),
    at: str = typer.Option("", "--at", help="Trace as of this time (RFC 3339); default now."),
    as_json: bool = typer.Option(False, "--json", help="Print the run as JSON."),
    explain: bool = typer.Option(
        True, "--explain/--no-explain", help="RCA where it stopped, and a cited answer."
    ),
    config: Path = _CONFIG,
) -> None:
    """Where is OBJECT_ID in FLOW, and is that where it should be?"""
    try:
        when = datetime.fromisoformat(at.replace("Z", "+00:00")) if at else None
        if when is not None and when.tzinfo is None:
            raise DomainValidationError("--at needs a timezone")  # noqa: TRY003, TRY301
        run, answer = trace_object(
            load_config_default(config), flow, object_id, at=when, explain=explain
        )
    except (ConfigError, DomainValidationError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except ExternalServiceError as exc:
        typer.echo(f"database error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if as_json:
        out = run.as_dict()
        if answer is not None:
            out["answer"] = answer.as_dict()
        typer.echo(json.dumps(out, indent=2))
        return
    _print_run(run)
    if answer is not None:
        _print_answer(answer)


def _print_run(run: TraceRun) -> None:
    v = run.verdict
    typer.echo(f"{run.flow} {run.object_id}: {v.kind}" + (f" at {v.stage}" if v.stage else ""))
    for o in run.observations:
        when_s = o.at.isoformat() if o.at else ""
        note = " rejected" if o.rejected else ""
        note += f" ({o.reason})" if o.reason else ""
        typer.echo(f"  {_MARK[o.state]} {o.stage:<20} {when_s}{note}")
    notes = [
        f"waiting since {v.since.isoformat()}" if v.since else "",
        f"late: {', '.join(v.late)}" if v.late else "",
        f"passed but not logged with the id (correlation gap): {', '.join(v.uncorrelated)}"
        if v.uncorrelated
        else "",
        f"can't tell: {v.reason}" if v.reason and v.kind == "cannot_tell" else "",
        f"evidence withheld from the model: {run.pii_error}" if run.pii_error else "",
    ]
    for note in notes:
        if note:
            typer.echo(f"  {note}")


def _print_answer(answer: TraceAnswer) -> None:
    typer.echo("")
    typer.echo(answer.summary)
    for s in answer.engineer:
        typer.echo(f"  - {s.text} [{', '.join(s.cites)}]")
    if answer.rca:
        typer.echo(f"  rca: {json.dumps(answer.rca)}")
    if answer.dropped:
        typer.echo(
            f"  ({answer.dropped} sentence(s) dropped: not supported by the facts they cite)"
        )


@flow_app.command("list")
def list_flows(config: Path = _CONFIG) -> None:
    """The flows in ``tracing.flows_dir``, reviewed or not."""
    try:
        flows = load_flows(load_config_default(config))
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not flows:
        typer.echo("no flows")
        return
    for flow in flows.values():
        state = "reviewed" if flow.reviewed else "draft (unreviewed: not used)"
        records = ", reads business records" if flow.reads_records() else ""
        typer.echo(f"{flow.flow:<28} {len(flow.stages)} stages, {state}{records}")
        typer.echo(f"  {flow.title}")


@flow_app.command("validate")
def validate(paths: list[Path] = typer.Argument(..., help="Flow files.")) -> None:  # noqa: B008
    """Check flow files without tracing anything."""
    failed = False
    for path in paths:
        try:
            flow = load_flow_file(path)
        except ConfigError as exc:
            typer.echo(f"FAIL {path}: {exc}")
            failed = True
            continue
        typer.echo(f"ok   {path}: {flow.flow}, {len(flow.stages)} stages")
    if failed:
        raise typer.Exit(2)
