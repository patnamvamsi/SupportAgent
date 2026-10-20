"""``supportagent stats [--mode shadow|assisted] [--window 30d] [--json]`` (02 §4).

Exit codes follow ``migrate``: 0 ok, 2 config or argument problem, 1 database.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

import typer

from supportagent.app.stats import Mode as StatsMode
from supportagent.app.stats import StatsReport, parse_window
from supportagent.bootstrap import build_stats
from supportagent.config.loader import load_config_default
from supportagent.domain.errors import ConfigError, DomainValidationError, ExternalServiceError


class Mode(StrEnum):
    SHADOW = "shadow"
    ASSISTED = "assisted"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _lines(r: StatsReport, window: str) -> list[str]:
    a, lat = r.agreement, r.first_proposal_latency_s
    out = [
        f"mode {r.mode}  window {window}  cases {r.n_cases}",
        f"agreement  {a.agreed}/{a.n}  rate {_pct(a.rate)}  wilson_lb {_pct(a.wilson_lb)}",
    ]
    for band, s in sorted(r.agreement_by_band.items()):
        out.append(
            f"  {band:<6} {s.agreed}/{s.n}  rate {_pct(s.rate)}  wilson_lb {_pct(s.wilson_lb)}"
        )
    out.append(f"first proposal  n {lat.n}  p50 {lat.p50}s  p95 {lat.p95}s")
    out.append(
        f"safe default {_pct(r.safe_default_rate)}  abstain {_pct(r.abstain_rate)}  "
        f"conflict {_pct(r.conflict_rate)}"
    )
    out.extend(f"  {reason:<28} {n}" for reason, n in r.safe_default_by_reason.items())
    return out


def stats(
    mode: Mode = typer.Option(Mode.SHADOW, "--mode", help="shadow or assisted (never mixed)."),  # noqa: B008
    window: str = typer.Option("30d", "--window", help="e.g. 30d, 12h, or all."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
    config: Path = typer.Option(  # noqa: B008
        "supportagent.yaml", "--config", help="Path to supportagent.yaml (02 §1)."
    ),
) -> None:
    """The mode-separated stats report (ARCH §8.4)."""
    try:
        span = parse_window(window)
        chosen: StatsMode = "assisted" if mode is Mode.ASSISTED else "shadow"
        report = build_stats(load_config_default(config)).report(window=span, mode=chosen)
    except (ConfigError, DomainValidationError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except ExternalServiceError as exc:
        typer.echo(f"database error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        typer.echo("\n".join(_lines(report, window)))
