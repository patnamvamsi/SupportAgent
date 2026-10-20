"""``supportagent eval run --suite smoke|invariants [--live] [--write-calibration]`` (02 §4).

Without ``--live`` the run is offline: the config is read without secrets
and the LLM is the fixture provider, which scripts each case's answer. It
checks wiring, and it's what CI runs. ``--live`` calls the configured model
(the config's ``*_env`` secrets must be set). ``--write-calibration``, live
only, writes ``evals/artifacts/calibration.json`` with ``grade: smoke``
(ADR-0002). Exit 0 pass, 1 gate fail, 2 usage/config error.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import typer

from supportagent.bootstrap import EvalPipeline, eval_config, load_eval_suite, wall_clock
from supportagent.domain.errors import ConfigError
from supportagent.evals.calibration import build_calibration
from supportagent.evals.harness import run_suite, select, write_report

app = typer.Typer(help="Eval suites (04 §8).", no_args_is_help=True)


@app.command("run")
def run(  # noqa: PLR0913, PLR0917 — one option per 02 §4 flag
    suite: str = typer.Option(..., "--suite", help="smoke | invariants"),
    live: bool = typer.Option(False, "--live", help="Call the configured LLM (needs secrets)."),
    write_calibration: bool = typer.Option(
        False, "--write-calibration", help="Write evals/artifacts/calibration.json (live only)."
    ),
    manifest: Path = typer.Option(  # noqa: B008
        Path("evals/manifest.yaml"), "--manifest", help="The eval manifest."
    ),
    config: Path | None = typer.Option(  # noqa: B008
        None, "--config", help="Config file; defaults to the manifest's."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON."),
    artifacts: Path | None = typer.Option(  # noqa: B008
        None, "--artifacts", help="Report directory; defaults to evals/artifacts."
    ),
) -> None:
    """Run one suite and write ``evals/artifacts/<run>/report.json``."""
    if write_calibration and not live:
        typer.echo("error: --write-calibration needs --live (fixtures can't calibrate)", err=True)
        raise typer.Exit(2)
    out_dir = artifacts or manifest.parent / "artifacts"
    try:
        cases, spec = load_eval_suite(manifest)
        if suite not in spec.suites:
            typer.echo(f"error: unknown suite {suite!r}; known: {', '.join(spec.suites)}", err=True)
            raise typer.Exit(2)
        cfg = eval_config(config or Path(spec.config), live=live, catalog=Path(spec.catalog))
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    selected = select(cases, spec.suites[suite])
    with tempfile.TemporaryDirectory() as workdir:
        pipeline = EvalPipeline(cfg, Path(workdir), live=live)
        try:
            result = run_suite(suite, selected, pipeline)
        finally:
            pipeline.close()
    now = wall_clock().now()
    report = write_report(out_dir, result, manifest=spec, model_id=pipeline.model_id, now=now)
    if write_calibration:
        table = build_calibration(
            result.pairs, model_id=pipeline.model_id, snapshot_id=spec.snapshot_id
        )
        target = out_dir / "calibration.json"
        target.write_text(table.model_dump_json(indent=2) + "\n", encoding="utf-8")
        typer.echo(f"calibration written: {target} ({len(table.labels)} labels, grade smoke)")
    body = result.score.as_dict()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
    else:
        for case in result.score.scores:
            mark = "PASS" if case.passed else ("FAIL" if case.gate else "MISS")
            typer.echo(f"{mark}  {case.case_id:<28} {'; '.join(case.failures)}")
        typer.echo(
            f"{suite} ({result.score.mode}): {body['cases_passed']}/{body['cases']} passed, "
            f"{len(result.score.skipped)} skipped; report {report}"
        )
    raise typer.Exit(0 if result.score.passed else 1)
