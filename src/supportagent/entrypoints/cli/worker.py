"""``supportagent worker`` and ``supportagent run`` (02 §4, 02 §7).

``worker`` runs the scheduled jobs only; ``--once`` runs each job a single
time and exits (cron-style deployments, smoke checks). ``run`` is the
single-container mode: the API and the worker in one process, the worker on a
background thread. SIGTERM / Ctrl-C finish the job in progress, then exit 0
(02 §4 "0 on SIGTERM drain"). Exit 2 on a config problem, like ``serve``.
"""

from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path
from types import FrameType

import typer
import uvicorn

from supportagent.bootstrap import build_api_context, build_worker
from supportagent.config.loader import load_config_default
from supportagent.domain.errors import ConfigError
from supportagent.entrypoints.api.app import create_app
from supportagent.entrypoints.worker.loop import Worker

_CONFIG_HELP = "Path to supportagent.yaml (02 §1)."


def _logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _load_worker(config: Path) -> Worker:
    try:
        return build_worker(load_config_default(config))
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc


def worker(
    config: Path = typer.Option("supportagent.yaml", "--config", help=_CONFIG_HELP),  # noqa: B008
    once: bool = typer.Option(False, "--once", help="Run each job once, then exit."),
) -> None:
    """Run the worker jobs (02 §7): the ServiceNow poll feeding shadow triage."""
    _logging()
    loop = _load_worker(config)
    if once:
        for result in loop.run_once():
            state = f"{result.rows} row(s)" if result.ran else "skipped (lease held elsewhere)"
            typer.echo(f"{result.name}: {state}")
        return

    def _drain(signum: int, frame: FrameType | None) -> None:
        loop.stop()

    signal.signal(signal.SIGTERM, _drain)
    signal.signal(signal.SIGINT, _drain)
    loop.run()


def run(
    config: Path = typer.Option("supportagent.yaml", "--config", help=_CONFIG_HELP),  # noqa: B008
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind."),
    port: int = typer.Option(8000, "--port", help="Port to bind."),
) -> None:
    """API and worker in one process (single-container mode, 02 §4)."""
    _logging()
    try:
        cfg = load_config_default(config)
        app = create_app(build_api_context(cfg))
        loop = build_worker(cfg)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    thread = threading.Thread(target=loop.run, name="supportagent-worker", daemon=True)
    thread.start()
    try:
        # uvicorn owns SIGTERM / SIGINT and returns once the API has drained.
        uvicorn.run(app, host=host, port=port, proxy_headers=True, server_header=False)
    finally:
        loop.stop()
        thread.join()
