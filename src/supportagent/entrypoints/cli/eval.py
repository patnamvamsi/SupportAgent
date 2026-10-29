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
    ONBOARDING_DIR,
    EvalPipeline,
    QualityPipeline,
    build_entail_pipeline,
    build_lab_pipeline,
    create_snapshot,
    detector_recall,
    eval_config,
    load_entail_suite,
    load_eval_suite,
    load_lab,
    load_onboarding_gates,
    load_reference_systems,
    quality_gates,
    split_problems,
    wall_clock,
)
from supportagent.config.loader import load_config_default, load_config_offline
from supportagent.config.schema import AppConfig
from supportagent.domain.errors import ConfigError
from supportagent.evals.calibration import build_calibration, build_holdout_calibration
from supportagent.evals.cases import EvalCase
from supportagent.evals.entail import run_entail
from supportagent.evals.harness import Manifest, run_suite, select, write_report
from supportagent.evals.lab import (
    read_baseline,
    run_lab,
    select_split,
    write_baseline,
    write_lab_report,
)
from supportagent.evals.onboarding import STAGES, evaluate, null, oracle, score, stage_passes
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


@app.command("lab")
def lab(  # noqa: PLR0913, PLR0917 — one option per flag
    pack: str = typer.Option("common", "--pack", help="The skill pack whose scenarios run."),
    live: bool = typer.Option(
        False, "--live", help="Call the configured model and record its answers (needs secrets)."
    ),
    samples: int = typer.Option(1, "--samples", min=1, help="Samples per scenario (majority)."),
    split: str = typer.Option("tune", "--split", help="tune | holdout | all"),
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.example.yaml"), "--config", help="Config file."
    ),
    write_baseline_flag: bool = typer.Option(
        False, "--write-baseline", help="Store this run as the pack's baseline."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON."),
    artifacts: Path = typer.Option(  # noqa: B008
        Path("evals/artifacts"), "--artifacts", help="Report directory."
    ),
) -> None:
    """The incident lab (ADR-0011 §5, WP-M02): a pack's scenarios with known root causes.

    Without ``--live`` the run replays the recorded responses under the pack's
    ``recordings/`` (deterministic, what CI runs); a response that isn't
    recorded fails closed and is reported. Exit 0 gates pass, 1 gate fail,
    2 usage or config error.
    """
    if split not in ("tune", "holdout", "all"):
        typer.echo("error: --split is tune, holdout or all", err=True)
        raise typer.Exit(2)
    try:
        cfg = load_config_default(config) if live else load_config_offline(config)
        scenarios, gates, directory = load_lab(cfg, pack)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    selected = select_split(scenarios, split)  # type: ignore[arg-type]
    if not selected:
        typer.echo(f"error: pack {pack!r} has no {split} scenarios", err=True)
        raise typer.Exit(2)
    pipeline = build_lab_pipeline(cfg, recordings=directory / "recordings", live=live, pack_id=pack)
    baseline_path = directory / "baseline.json"
    report = run_lab(
        selected,
        pipeline,
        pack=pack,
        mode="live" if live else "replay",
        model_id=pipeline.model_id,
        samples=samples,
        gates=gates,
        baseline=read_baseline(baseline_path),
    )
    path = write_lab_report(artifacts, report, now=wall_clock().now())
    misses = pipeline.caching.misses if pipeline.caching is not None and not live else 0
    if write_baseline_flag:
        write_baseline(baseline_path, report)
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
    else:
        for score in report.scores:
            mark = "PASS" if score.passed else "FAIL"
            typer.echo(f"{mark}  {score.scenario_id:<44} {'; '.join(score.failures)}")
        m = report.metrics()
        typer.echo(
            f"lab {pack} ({report.mode}, {samples} sample(s)): {m['passed']}/{m['cases']} passed; "
            f"false causes {m['false_cause']}; report {path}"
        )
        for gate in report.gates:
            typer.echo(f"  gate {gate.name}: {'ok' if gate.ok else 'FAIL'} — {gate.detail}")
        if misses:
            typer.echo(f"  {misses} response(s) were not recorded: run with --live to record")
    raise typer.Exit(0 if report.passed and not misses else 1)


@app.command("entail")
def entail(
    live: bool = typer.Option(
        False, "--live", help="Call the configured model and record its answers (needs secrets)."
    ),
    samples: int = typer.Option(1, "--samples", min=1, help="Samples per pair (majority)."),
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.example.yaml"), "--config", help="Config file."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON."),
) -> None:
    """Citation faithfulness (BL-039): the ``entail`` checker on curated claim/evidence pairs.

    Without ``--live`` the run replays ``evals/entail/recordings`` (what CI
    runs); an unrecorded pair fails closed. Exit 0 gates pass, 1 gate fail or
    unrecorded responses, 2 config error.
    """
    try:
        cfg = load_config_default(config) if live else load_config_offline(config)
        cases, gates = load_entail_suite(cfg)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    pipeline = build_entail_pipeline(cfg, live=live)
    report = run_entail(
        cases,
        pipeline.judge,
        mode="live" if live else "replay",
        model_id=pipeline.model_id,
        samples=samples,
        gates=gates,
    )
    misses = pipeline.caching.misses if pipeline.caching is not None and not live else 0
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
    else:
        for score in report.scores:
            mark = "ok  " if score.ok else "MISS"
            typer.echo(f"{mark}  {score.case_id:<44} expected {score.expected}, got {score.got}")
        typer.echo(
            f"entail ({report.mode}, {samples} sample(s)): faithfulness "
            f"{report.faithfulness:.3f} over {len(report.scores)} pair(s); "
            f"false supports {report.false_supports}"
        )
        for gate in report.gates:
            typer.echo(f"  gate {gate.name}: {'ok' if gate.ok else 'FAIL'} — {gate.detail}")
        if misses:
            typer.echo(f"  {misses} response(s) were not recorded: run with --live to record")
    raise typer.Exit(0 if report.passed and not misses else 1)


@app.command("onboarding")
def onboarding(
    root: Path = typer.Option(  # noqa: B008
        ONBOARDING_DIR, "--root", help="Directory of reference systems (ADR-0015)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the scores as JSON."),
) -> None:
    """The onboarding lab (WP-O7): each reference system is checked and the scorer self-tested.

    For each system under ``--root``: the fixture must be self-consistent
    (every truth fact cites a source the fixture contains), the **oracle**
    result (the truth) must pass every gate, and the **null** result must
    fail every stage. Once a pipeline stage exists it is scored here too, and
    its gates bind when ``gates.yaml`` lists it in ``enforced_stages``.
    Exit 0 when all of that holds, 1 otherwise, 2 for an unreadable fixture.
    """
    try:
        systems = load_reference_systems(root)
        gates = load_onboarding_gates(root)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    failed = False
    report: list[dict[str, object]] = []
    for system in systems:
        problems = system.problems()
        oracle_scores = score(system, oracle(system))
        oracle_gates = evaluate(oracle_scores, gates)
        null_gates = evaluate(score(system, null()), gates)
        null_bites = [stage for stage in STAGES if not stage_passes(null_gates, stage)]
        ok = not problems and all(g.ok for g in oracle_gates) and len(null_bites) == len(STAGES)
        failed |= not ok
        weights = system.class_weights()
        report.append(
            {
                "system": system.id,
                "ok": ok,
                "fixture_problems": problems,
                "oracle_failed_gates": [g.detail for g in oracle_gates if not g.ok],
                "null_passing_stages": [s for s in STAGES if s not in null_bites],
                "incident_classes": weights,
                "inventory": {
                    "confluence_pages": len(system.inventory.confluence),
                    "git_files": len(system.inventory.git),
                    "cmdb_records": len(system.inventory.cmdb),
                    "incidents": len(system.inventory.incidents),
                },
            }
        )
        if not as_json:
            mark = "ok  " if ok else "FAIL"
            inv = system.inventory
            typer.echo(
                f"{mark}  {system.id:<20} {len(inv.confluence)} pages, {len(inv.git)} repo files, "
                f"{len(inv.incidents)} incidents in {len(weights)} classes; "
                f"graph {len(system.truth.graph.nodes)} nodes / "
                f"{len(system.truth.graph.edges)} edges"
            )
            for problem in problems:
                typer.echo(f"      fixture: {problem}")
            for gate in oracle_gates:
                if not gate.ok:
                    typer.echo(f"      oracle fails {gate.detail}")
            for stage in STAGES:
                if stage not in null_bites:
                    typer.echo(f"      null passes {stage}: its gates don't bite")
    if as_json:
        typer.echo(
            json.dumps({"systems": report, "enforced_stages": gates.enforced_stages}, indent=2)
        )
    else:
        enforced = ", ".join(gates.enforced_stages) or "none yet (they land with WP-O1..O6)"
        typer.echo(
            f"onboarding lab: {len(systems)} system(s); stages enforced on the pipeline: {enforced}"
        )
    raise typer.Exit(1 if failed or not systems else 0)
