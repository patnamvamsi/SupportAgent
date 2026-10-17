"""``supportagent`` CLI (typer). v0.0 surface: ``--version`` (WP-000).

Commands for triage, doctor, evals and run land with their WPs (10-wp).
"""

from __future__ import annotations

import typer

import supportagent


def _print_version(value: bool) -> None:
    if value:
        typer.echo(supportagent.__version__)
        raise typer.Exit()


app = typer.Typer(
    name="supportagent",
    help="SupportAgent — an application support analyst.",
    no_args_is_help=True,
)


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
