"""``supportagent`` CLI (typer). v0.0 surface: ``--version`` (WP-000),
``migrate`` (WP-003), ``replay`` (WP-004), ``grants request`` (WP-012),
``serve`` (WP-013), ``stats`` / ``shadow import`` (WP-014), ``worker`` /
``run``, ``doctor`` (WP-015), ``eval run`` (WP-016); ``pack``, ``tool`` and
``scenario`` generators (WP-M09).
"""

from __future__ import annotations

import typer

import supportagent
from supportagent.entrypoints.cli import demo_servicenow as demo_sn_command
from supportagent.entrypoints.cli import doctor as doctor_command
from supportagent.entrypoints.cli import eval as eval_command
from supportagent.entrypoints.cli import extend as extend_command
from supportagent.entrypoints.cli import grants as grants_command
from supportagent.entrypoints.cli import init as init_command
from supportagent.entrypoints.cli import migrate as migrate_command
from supportagent.entrypoints.cli import plugin as plugin_command
from supportagent.entrypoints.cli import replay as replay_command
from supportagent.entrypoints.cli import serve as serve_command
from supportagent.entrypoints.cli import shadow as shadow_command
from supportagent.entrypoints.cli import sop as sop_command
from supportagent.entrypoints.cli import stats as stats_command
from supportagent.entrypoints.cli import trace as trace_command
from supportagent.entrypoints.cli import worker as worker_command


def _print_version(value: bool) -> None:
    if value:
        typer.echo(supportagent.__version__)
        raise typer.Exit()


app = typer.Typer(
    name="supportagent",
    help="SupportAgent — an application support analyst.",
    no_args_is_help=True,
)

# 02 §4: ``supportagent migrate [--to head]`` — a single command on the root
# app (not a sub-command group), so the spec's invocation is the real one.
app.command("migrate")(migrate_command.migrate)
# 02 §4: ``supportagent replay <trace-id> [--json]`` (WP-004).
app.command("replay")(replay_command.replay)
# 02 §4: ``supportagent doctor`` (WP-015).
app.command("doctor")(doctor_command.doctor)
# The local installer (./install.sh): the setup wizard and the demo ServiceNow.
app.command("init")(init_command.init)
app.command("demo-servicenow")(demo_sn_command.demo_servicenow)
# 02 §4: ``supportagent serve`` — the API + admin UI (WP-013).
app.command("serve")(serve_command.serve)
# 02 §4: ``supportagent worker`` (jobs only) and ``run`` (API + worker).
app.command("worker")(worker_command.worker)
app.command("run")(worker_command.run)
# 02 §4: ``supportagent stats`` and ``shadow import`` (WP-014).
app.command("stats")(stats_command.stats)
app.add_typer(shadow_command.app, name="shadow")
# 02 §4: ``supportagent eval run --suite smoke|invariants`` (WP-016).
app.add_typer(eval_command.app, name="eval")
# 02 §4: ``supportagent grants request --from config`` (WP-012).
app.add_typer(grants_command.app, name="grants")
# WP-M09: analyst extensions (``pack new|validate``, ``tool new``, ``scenario new``).
app.add_typer(extend_command.pack_app, name="pack")
app.add_typer(extend_command.tool_app, name="tool")
app.add_typer(extend_command.scenario_app, name="scenario")
app.add_typer(extend_command.runbook_app, name="runbook")
# ADR-0014 part 2 (BL-054): ``sop list`` and ``sop auto``.
app.add_typer(sop_command.app, name="sop")
# ADR-0021: transaction tracing.
app.command("trace")(trace_command.trace)
app.add_typer(trace_command.flow_app, name="flow")
# ADR-0015 (WP-O1): ``plugin list|show|validate|index`` and ``onboard detect``.
app.add_typer(plugin_command.plugin_app, name="plugin")
app.add_typer(plugin_command.onboard_app, name="onboard")


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_print_version,
        is_eager=True,
        help="Print the version and exit.",
    ),
) -> None:
    """SupportAgent — an application support analyst."""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
