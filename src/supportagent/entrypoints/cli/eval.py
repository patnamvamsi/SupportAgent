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

from supportagent.bootstrap import (
    EvalPipeline,
    QualityPipeline,
    create_snapshot,
    detector_recall,
    eval_config,
    load_eval_suite,
    quality_gates,
    split_problems,
    wall_clock,
)
from supportagent.config.loader import load_config_offline
from supportagent.config.schema import AppConfig
from supportagent.domain.errors import ConfigError
from supportagent.evals.calibration import build_calibration, build_holdout_calibration
from supportagent.evals.cases import EvalCase
from supportagent.evals.harness import Manifest, run_suite, select, write_report
from supportagent.evals.quality import baseline_from, run_quality, write_quality_report

app = typer.Typer(help="Eval suites (04 §8).", no_args_is_help=True)


@app.command("run")
def run(  # noqa: PLR0913, PLR0917 — one option per 02 §4 flag
    suite: str = typer.Option(..., "--suite", help="smoke | invariants | quality"),
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
    samples: int = typer.Option(3, "--samples", min=1, help="Quality: samples per case."),
    snapshot: str | None = typer.Option(
        None, "--snapshot", help="Quality: pin a frozen snapshot (evals/snapshots/<id>)."
    ),
    baseline: Path | None = typer.Option(  # noqa: B008
        None, "--baseline", help="Quality: an earlier report.json for the per-case diff."
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
    if suite == "quality":
        _quality(
            cfg,
            selected,
            spec=spec,
            live=live,
            samples=samples,
            snapshot=snapshot,
            baseline=baseline,
            root=manifest.parent,
            out_dir=out_dir,
            as_json=as_json,
            write_calibration=write_calibration,
        )
        return
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
            result.pairs,
            model_id=pipeline.model_id,
            snapshot_id=spec.snapshot_id,
            report_id=report.parent.name,
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


def _quality(  # noqa: PLR0913 — the quality run's inputs
    cfg: AppConfig,
    cases: list[EvalCase],
    *,
    spec: Manifest,
    live: bool,
    samples: int,
    snapshot: str | None,
    baseline: Path | None,
    root: Path,
    out_dir: Path,
    as_json: bool,
    write_calibration: bool = False,
) -> None:
    """ARCH §11 quality suite: N samples, per-case majority, LB over cases, gates."""
    snapshot_id = snapshot or spec.snapshot_id
    if snapshot is not None and not (root / "snapshots" / snapshot).is_dir():
        typer.echo(f"error: no snapshot {snapshot!r} under {root / 'snapshots'}", err=True)
        raise typer.Exit(2)
    splits = root / "splits.yaml"
    problems = split_problems(splits, cases) if splits.is_file() else []
    if problems:
        typer.echo("error: the holdout split is not disjoint: " + "; ".join(problems), err=True)
        raise typer.Exit(2)
    sampler = QualityPipeline(cfg, live=live, snapshot_id=snapshot_id, cache_dir=root / ".cache")
    heldout = root / "secrets_heldout" / "heldout.jsonl"
    report = run_quality(
        cases,
        sampler,
        samples=samples,
        snapshot_id=snapshot_id,
        gates=quality_gates(cfg),
        baseline=baseline_from(baseline) if baseline else None,
        recall=detector_recall(heldout) if heldout.is_file() else None,
    )
    path = write_quality_report(out_dir, report, now=wall_clock().now())
    if write_calibration:
        indexes = {c.id: i for i, c in enumerate(cases)}
        table = build_holdout_calibration(
            report.outcomes,
            {
                o.case_id: sampler.preclass(cases[indexes[o.case_id]], indexes[o.case_id])
                for o in report.outcomes
            },
            model_id=sampler.model_id,
            snapshot_id=snapshot_id,
            report_id=path.parent.name,
        )
        target = out_dir / "calibration.json"
        target.write_text(table.model_dump_json(indent=2) + "\n", encoding="utf-8")
        typer.echo(f"calibration written: {target} ({len(table.labels)} labels, grade holdout)")
    hits, misses = sampler.cache_stats
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
    else:
        typer.echo((path.parent / "summary.md").read_text(encoding="utf-8"))
        typer.echo(f"cache: {hits} hit(s), {misses} miss(es); report {path}")
    raise typer.Exit(0 if report.passed else 1)


snapshot_app = typer.Typer(help="Frozen eval snapshots (ARCH §11).", no_args_is_help=True)
app.add_typer(snapshot_app, name="snapshot")


@snapshot_app.command("create")
def snapshot_create(
    snapshot_id: str = typer.Option(..., "--id", help="Snapshot id, e.g. snap-2026-11-02."),
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="Config whose catalog/taxonomy to freeze."
    ),
    root: Path = typer.Option(Path("evals/snapshots"), "--root", help="Snapshot directory."),  # noqa: B008
) -> None:
    """Freeze the catalog, taxonomies, pre-classifier rules and prompts (immutable)."""
    try:
        out = create_snapshot(load_config_offline(config), snapshot_id, root)
    except (ConfigError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"snapshot written: {out}")
