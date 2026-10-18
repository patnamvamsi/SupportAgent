"""``supportagent`` CLI (typer). v0.0 surface: ``--version`` (WP-000),
``migrate`` (WP-003). Commands for triage, doctor, evals and run land with
their WPs (10-wp).
"""

from __future__ import annotations

import typer

import supportagent
from supportagent.entrypoints.cli import migrate as migrate_command


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
