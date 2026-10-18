"""``supportagent replay <trace-id> [--json]`` (02 §4, 10-wp WP-004).

An ordered event timeline with tokens and latencies: ``ts``, ``kind``,
``actor``, ``status``, ``model``, ``tokens``, ``latency`` plus the one-line
payload summary from the per-kind formatter registry (``app.replay``).

The database URL and master key are never CLI arguments (02 §1, 05 §6): the
URL comes from the config file's ``database.url_env``, the master key from
``SA_MASTER_KEY_ENV`` (03 §3) — both resolved in ``config.loader``. This
module only renders and maps to the 02 §4 exit codes: 0 ok, 4 not found
(config problems: 2, database problems: 1, as with ``migrate``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import typer

from supportagent.app.replay import ReplayView, summarize
from supportagent.bootstrap import replay_trace
from supportagent.config.loader import load_config_default, load_master_key_default
from supportagent.domain.errors import (
    ConfigError,
    DomainValidationError,
    ExternalServiceError,
    NotFound,
)
from supportagent.domain.model.event import Event

_HEADERS = ("TS", "KIND", "ACTOR", "STATUS", "MODEL", "TOKENS", "LAT_MS", "SUMMARY")


def _json_row(event: Event) -> dict[str, object]:
    """One JSON row: the 02 §4 columns plus the one-line payload summary."""
    return {
        "ts": event.ts.isoformat(),
        "kind": event.kind.value,
        "actor": event.actor,
        "status": event.status,
        "model": event.model,
        "tokens": {"in": event.tokens_in, "out": event.tokens_out},
        "latency_ms": event.latency_ms,
        "span_id": event.span_id,
        "parent_span_id": event.parent_span_id,
        "summary": summarize(event.kind, event.payload),
    }


def _table(view: ReplayView) -> Iterator[str]:
    rows = [
        (
            e.ts.isoformat(),
            e.kind.value,
            e.actor,
            e.status,
            e.model or "-",
            f"{e.tokens_in}/{e.tokens_out}",
            str(e.latency_ms),
            summarize(e.kind, e.payload),
        )
        for e in view.events
    ]
    widths = [
        max(len(header), *(len(row[i]) for row in rows)) if rows else len(header)
        for i, header in enumerate(_HEADERS)
    ]
    yield "  ".join(h.ljust(widths[i]) for i, h in enumerate(_HEADERS)).rstrip()
    for row in rows:
        yield "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()


def replay(
    trace_id: str = typer.Argument(help="The trace id to replay (02 §4)."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table (02 §4)."),
    # B008: typer.Option defaults are the Typer idiom (see migrate.py).
    config: Path = typer.Option(  # noqa: B008
        "supportagent.yaml",
        "--config",
        help="Path to supportagent.yaml (02 §1).",
    ),
) -> None:
    """Replay one trace: an ordered event timeline with tokens and latencies."""
    try:
        app_config = load_config_default(config)
        master_key = load_master_key_default()
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        view = replay_trace(app_config, master_key, trace_id)
    except NotFound as exc:
        typer.echo(f"not found: {exc}", err=True)
        raise typer.Exit(4) from exc
    except DomainValidationError as exc:
        typer.echo(f"domain error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except ExternalServiceError as exc:
        typer.echo(f"database error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if as_json:
        payload = {
            "trace_id": view.trace_id,
            "tenant_id": view.tenant_id,
            "events": [_json_row(event) for event in view.events],
        }
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    for line in _table(view):
        typer.echo(line)
