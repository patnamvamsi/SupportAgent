"""``supportagent doctor [--setup] [--discover-automation] [--json]`` (02 §4, §4.1).

Runs every 02 §4.1 check and prints one line per check. Exit codes (02 §4):
0 all pass, 1 any fail, 3 warnings only. A config file that doesn't load is
itself the ``config`` failure: it is reported, not raised.

``--setup`` then writes the default T1 grant requests (ARCH §6.4) if the
database check passed. ``--discover-automation`` writes candidate
``automation_actors`` rows for an admin to confirm. Nothing is ever written
to ServiceNow.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from supportagent.app.doctor import CheckResult, DoctorReport
from supportagent.bootstrap import request_default_grants, run_doctor
from supportagent.config.loader import load_config_default
from supportagent.config.schema import AppConfig
from supportagent.domain.errors import ConfigError, ExternalServiceError

_MARK = {status: status.upper() for status in ("pass", "warn", "fail")}


def doctor(
    config: Path = typer.Option(  # noqa: B008
        "supportagent.yaml", "--config", help="Path to supportagent.yaml (02 §1)."
    ),
    setup: bool = typer.Option(False, "--setup", help="Write the default T1 grant requests."),
    discover_automation: bool = typer.Option(
        False, "--discover-automation", help="Record non-interactive actors for confirmation."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON."),
) -> None:
    """Check the deployment: config, database, ServiceNow, paging, LLM, identity."""
    extra: dict[str, object] = {}
    try:
        cfg = load_config_default(config)
    except ConfigError as exc:
        report = DoctorReport(results=[CheckResult("config", "fail", str(exc))])
    else:
        report = run_doctor(cfg, discover_automation=discover_automation)
        if setup:
            extra["setup"] = _setup(cfg, report)
    if as_json:
        typer.echo(json.dumps({**report.as_dict(), **extra}, indent=2))
    else:
        for result in report.results:
            typer.echo(f"{_MARK[result.status]}  {result.name:<30} {result.detail}")
        if "setup" in extra:
            typer.echo(f"setup: {extra['setup']}")
    raise typer.Exit(report.exit_code)


def _setup(cfg: AppConfig, report: DoctorReport) -> str:
    db = next((r for r in report.results if r.name == "db"), None)
    if db is None or db.status != "pass":
        return "skipped: the db check did not pass"
    try:
        created = request_default_grants(cfg)
    except (ConfigError, ExternalServiceError) as exc:
        return f"failed: {exc}"
    return f"requested {len(created)} default T1 grant(s); approve them at /ui/setup"
