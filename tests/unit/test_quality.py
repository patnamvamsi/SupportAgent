"""WP-104: the quality eval harness (ARCH §11; 01 §7.6, §7.9).

Canonical test IDs from 11-wp §WP-104: ``test_majority_vote_per_case``,
``test_wilson_over_cases_not_samples``, ``test_cost_matrix_totals``,
``test_cache_key_includes_snapshot_and_resolved_model``,
``test_cache_invalidated_on_rendered_prompt_change``,
``test_gate_report_fails_below_threshold``, ``test_per_case_diff_against_baseline``,
``test_split_disjoint_by_time_and_signature``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from supportagent.adapters.llm.cache import CachingLLMProvider
from supportagent.bootstrap import create_snapshot, detector_recall, eval_config
from supportagent.domain.quality import (
    BandScore,
    CaseOutcome,
    QualityGate,
    SplitCase,
    band_scores,
    cost_total,
    diff_against_baseline,
    evaluate_gates,
    majority,
    split_violations,
)
from supportagent.domain.stats import wilson_lower_bound
from supportagent.entrypoints.cli.main import app
from supportagent.ports.llm import ProviderRequest, ProviderResponse

_REPO = Path(__file__).resolve().parents[2]
_MODEL = "claude-sonnet-4-5-20250929"


def _outcome(
    case_id: str, truth: tuple[str, str], *samples: tuple[str | None, str | None]
) -> CaseOutcome:
    return CaseOutcome(case_id, truth[0], truth[1], list(samples))


def test_majority_vote_per_case() -> None:
    assert majority(["a", "b", "a"]) == "a"
    assert majority(["b", "a", "a", "b"]) == "b", "ties go to the earliest sample"
    assert majority([None, None]) is None
    o = _outcome(
        "c1",
        ("database/saturation", "P2"),
        ("database/failure", "P2"),
        ("network/latency", "P3"),
        ("database/failure", "P2"),
    )
    assert (o.classification, o.severity) == ("database/failure", "P2")
    assert o.classification_correct, "prefix match on the majority label"
    assert o.severity_correct


def test_wilson_over_cases_not_samples() -> None:
    # 40 cases x 5 samples, each case right by majority: n is 40, not 200.
    outcomes = [
        _outcome(f"c{i}", ("db/x", "P1"), *[("db/x", "P1")] * 4, ("net/y", "P3")) for i in range(40)
    ]
    p1p2 = band_scores(outcomes)["P1-P2"]
    assert (p1p2.cases, p1p2.classification_correct) == (40, 40)
    assert p1p2.classification_lb == pytest.approx(wilson_lower_bound(40, 40))
    assert p1p2.classification_lb < wilson_lower_bound(200, 200)


def test_cost_matrix_totals() -> None:
    outcomes = [
        _outcome("a", ("db/x", "P1"), ("db/x", "P3")),  # P1 -> P3: 8
        _outcome("b", ("db/x", "P3"), ("db/x", "P1")),  # P3 -> P1: 1
        _outcome("c", ("db/x", "P2"), ("db/x", "P2")),  # exact: 0
        _outcome("d", ("db/x", "P4"), (None, None)),  # no prediction: worst of row (2)
    ]
    assert [o.cost for o in outcomes] == [8, 1, 0, 2]
    assert cost_total(outcomes) == 11


def _request(user: str = "ticket body", model: str = _MODEL) -> ProviderRequest:
    return ProviderRequest(
        model_id=model, system="s", user=user, json_schema={}, max_output_tokens=50, timeout_s=5
    )


class _Counting:
    def __init__(self, resolved: str | None = None) -> None:
        self.calls = 0
        self.resolved = resolved

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        return ProviderResponse(
            raw_text="{}",
            resolved_model=self.resolved or request.model_id,
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )


def test_cache_key_includes_snapshot_and_resolved_model(tmp_path: Path) -> None:
    inner = _Counting()
    snap_a = CachingLLMProvider(inner, directory=tmp_path, snapshot_id="snap-a")
    snap_a.complete_json(_request())
    snap_a.complete_json(_request())
    assert (inner.calls, snap_a.hits) == (1, 1)
    snap_b = CachingLLMProvider(inner, directory=tmp_path, snapshot_id="snap-b")
    snap_b.complete_json(_request())
    assert inner.calls == 2, "another snapshot is another key"
    # A provider that resolves the pinned id to a different version is never cached.
    remapped = _Counting(resolved="claude-sonnet-4-5-20251101")
    other = CachingLLMProvider(remapped, directory=tmp_path / "x", snapshot_id="snap-a")
    other.complete_json(_request())
    other.complete_json(_request())
    assert remapped.calls == 2
    # Each of the N samples is its own entry.
    snap_a.sample = 1
    snap_a.complete_json(_request())
    assert inner.calls == 3


def test_cache_invalidated_on_rendered_prompt_change(tmp_path: Path) -> None:
    inner = _Counting()
    cache = CachingLLMProvider(inner, directory=tmp_path, snapshot_id="snap-a")
    cache.complete_json(_request("ticket body"))
    cache.complete_json(_request("ticket body, plus a newly retrieved similar case"))
    assert inner.calls == 2


def test_gate_report_fails_below_threshold() -> None:
    gates = (QualityGate("p1p2_classification", "P1-P2", "classification", 0.90, 35),)
    strong = {"P1-P2": BandScore("P1-P2", 60, 60, 58)}
    weak = {"P1-P2": BandScore("P1-P2", 52, 45, 50)}
    thin = {"P1-P2": BandScore("P1-P2", 20, 20, 20)}
    assert evaluate_gates(strong, gates)[0].passed
    (low,) = evaluate_gates(weak, gates)
    assert not low.passed and "<" in low.reason
    (few,) = evaluate_gates(thin, gates)
    assert not few.passed and "needs >= 35" in few.reason


def test_per_case_diff_against_baseline() -> None:
    diff = diff_against_baseline(
        {"a": True, "b": False, "c": True, "new": True},
        {"a": True, "b": True, "c": False, "gone": True},
    )
    assert (diff.newly_failing, diff.newly_passing) == (["b"], ["c"])
    assert (diff.added, diff.removed) == (["new"], ["gone"])


def test_split_disjoint_by_time_and_signature() -> None:
    t = datetime(2026, 6, 1, tzinfo=UTC)
    train = [SplitCase("t1", t, "5xx:/a"), SplitCase("t2", t + timedelta(days=1), "disk:b")]
    clean = [SplitCase("h1", t + timedelta(days=5), "latency:c")]
    assert split_violations(train, clean) == []
    leaky = [SplitCase("h2", t, "other"), SplitCase("h3", t + timedelta(days=9), "5xx:/a")]
    problems = split_violations(train, leaky)
    assert len(problems) == 2
    assert "h2" in problems[0] and "h3" in problems[1]


def test_detector_recall_is_measured_on_the_heldout_corpus() -> None:
    report = detector_recall(_REPO / "evals" / "secrets_heldout" / "heldout.jsonl")
    assert report.total == 10
    assert 0 < report.detected <= report.total
    assert set(report.by_class) >= {"email", "phone_e164", "card_number"}


def test_snapshot_freezes_inputs(tmp_path: Path) -> None:
    cfg = eval_config(
        _REPO / "supportagent.example.yaml",
        live=False,
        catalog=_REPO / "catalog" / "services.example.yaml",
    )
    out = create_snapshot(cfg, "snap-test-1", tmp_path)
    manifest = json.loads((out / "manifest.json").read_text())
    assert {"catalog.yaml", "classification.yaml", "preclass_rules.yaml"} <= set(manifest["files"])
    assert any(name.startswith("prompts/") for name in manifest["files"])
    with pytest.raises(Exception, match="immutable"):
        create_snapshot(cfg, "snap-test-1", tmp_path)


def test_quality_cli_runs_offline_and_writes_summary(tmp_path: Path) -> None:
    """Holdout-tagged cases through ``eval run --suite quality`` (fixture LLM)."""
    import shutil  # noqa: PLC0415

    root = tmp_path / "evals"
    (root / "cases").mkdir(parents=True)
    for src in sorted((_REPO / "evals" / "cases").glob("db-sat-*.yaml")):
        text = src.read_text(encoding="utf-8").replace("- routing\n", "- routing\n- holdout\n", 1)
        (root / "cases" / src.name).write_text(text, encoding="utf-8")
    manifest = (_REPO / "evals" / "manifest.yaml").read_text(encoding="utf-8")
    manifest = manifest.replace(
        "config: supportagent.example.yaml", f"config: {_REPO / 'supportagent.example.yaml'}"
    )
    manifest = manifest.replace(
        "catalog: catalog/services.example.yaml",
        f"catalog: {_REPO / 'catalog' / 'services.example.yaml'}",
    )
    (root / "manifest.yaml").write_text(manifest, encoding="utf-8")
    shutil.copytree(_REPO / "evals" / "secrets_heldout", root / "secrets_heldout")
    out = CliRunner().invoke(
        app,
        [
            "eval",
            "run",
            "--suite",
            "quality",
            "--samples",
            "3",
            "--manifest",
            str(root / "manifest.yaml"),
            "--artifacts",
            str(tmp_path / "out"),
            "--json",
        ],
    )
    assert out.exit_code == 1, "12 cases can't pass a gate that needs 35 (honest fail)"
    body = json.loads(out.output)
    assert body["cases"] == 12 and body["samples"] == 3
    assert body["bands"]["P1-P2"]["cases"] + body["bands"]["P3-P4"]["cases"] == 12
    assert all(row["passed"] for row in body["per_case"]), "the fixture answers the truth"
    (summary,) = (tmp_path / "out").glob("*/summary.md")
    assert "needs >= 35" in summary.read_text()


def test_quality_cli_refuses_a_leaky_split(tmp_path: Path) -> None:
    root = tmp_path / "evals"
    (root / "cases").mkdir(parents=True)
    for name in ("db-sat-01.yaml", "db-sat-02.yaml"):
        text = (_REPO / "evals" / "cases" / name).read_text(encoding="utf-8")
        (root / "cases" / name).write_text(
            text.replace("- routing\n", "- routing\n- holdout\n", 1), encoding="utf-8"
        )
    manifest = (_REPO / "evals" / "manifest.yaml").read_text(encoding="utf-8")
    manifest = manifest.replace(
        "config: supportagent.example.yaml", f"config: {_REPO / 'supportagent.example.yaml'}"
    )
    manifest = manifest.replace(
        "catalog: catalog/services.example.yaml",
        f"catalog: {_REPO / 'catalog' / 'services.example.yaml'}",
    )
    (root / "manifest.yaml").write_text(manifest, encoding="utf-8")
    # db-sat-02 is received after db-sat-01, so it can't be training data for it.
    (root / "splits.yaml").write_text(
        "train: [db-sat-02]\nholdout: [db-sat-01]\n", encoding="utf-8"
    )
    out = CliRunner().invoke(
        app,
        [
            "eval",
            "run",
            "--suite",
            "quality",
            "--manifest",
            str(root / "manifest.yaml"),
            "--artifacts",
            str(tmp_path / "out"),
        ],
    )
    assert out.exit_code == 2
    assert "not disjoint" in out.output
