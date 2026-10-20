"""WP-016: the smoke harness, scoring, calibration and the spec test-ID index.

Canonical test IDs from 10-wp §WP-016: ``test_harness_scores_golden_run``,
``test_calibration_written_with_grade_and_model``,
``test_injection_canary_case_not_followed``, ``test_spec_test_ids_exist``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from supportagent.bootstrap import EvalPipeline, eval_config, load_eval_suite
from supportagent.domain.model.triage import TriageDecision
from supportagent.entrypoints.cli.main import app
from supportagent.evals.calibration import build_calibration
from supportagent.evals.cases import EvalCase
from supportagent.evals.harness import Manifest, run_suite, select
from supportagent.evals.scoring import CaseRun, score

_REPO = Path(__file__).resolve().parents[2]
_MANIFEST = _REPO / "evals" / "manifest.yaml"


@pytest.fixture(scope="module")
def suite() -> tuple[list[EvalCase], Manifest]:
    return load_eval_suite(_MANIFEST)


@pytest.fixture
def pipeline(tmp_path: Path, suite: tuple[list[EvalCase], Manifest]) -> Any:
    _, manifest = suite
    cfg = eval_config(_REPO / manifest.config, live=False, catalog=_REPO / manifest.catalog)
    pipe = EvalPipeline(cfg, tmp_path, live=False)
    yield pipe
    pipe.close()


def _case(cases: list[EvalCase], case_id: str) -> EvalCase:
    return next(c for c in cases if c.id == case_id)


def test_suite_shape(suite: tuple[list[EvalCase], Manifest]) -> None:
    cases, manifest = suite
    assert 25 <= len(cases) <= 50
    routing = [c for c in cases if c.expect.classification == "database/saturation"]
    assert len(routing) >= 12, "≥ 12 cases for the v0.0 routing label (04 §8)"
    tags = {t for c in cases for t in c.tags}
    assert {"fault", "injection", "redaction", "invariant"} <= tags
    assert any(c.repeat > 1 for c in cases), "a duplicate case"
    assert all(c.meta.get("why") for c in cases), "every case says why (ROADMAP §2.1)"
    assert set(manifest.suites) == {"smoke", "invariants", "quality"}


def test_harness_scores_golden_run(
    pipeline: Any,
    suite: tuple[list[EvalCase], Manifest],
) -> None:
    cases, manifest = suite
    result = run_suite("smoke", select(cases, manifest.suites["smoke"]), pipeline)
    failing = {s.case_id: s.failures for s in result.score.scores if not s.passed}
    assert not failing, failing
    assert result.score.passed and result.score.skipped == []
    # The decisions are the pipeline's, not echoes of the expectations.
    by_id = {case.id: run for case, run in result.pairs}
    assert by_id["db-sat-04"].decision.outcome == "routed"
    assert by_id["db-sat-04"].decision.route_id == "db-saturation"
    assert by_id["db-sat-06"].decision.route_id == "generic", "P4 falls through to the default"
    assert by_id["llm-timeout-01"].decision.reason == "llm-failure"
    assert by_id["duplicate-01"].linked


def test_harness_fails_wrong_expectations(
    pipeline: Any,
    suite: tuple[list[EvalCase], Manifest],
) -> None:
    cases, _ = suite
    base = _case(cases, "db-sat-01")
    wrong = base.model_copy(
        update={
            "id": "db-sat-wrong",
            "fixture": base.fixture_answer(),  # the LLM still answers the true labels
            "expect": base.expect.model_copy(
                update={"classification": "network/latency", "severity": "P1", "actions": []}
            ),
        }
    )
    result = run_suite("smoke", [wrong], pipeline)
    (only,) = result.score.scores
    assert not only.passed and not result.score.passed
    assert [f.split()[0] for f in only.failures] == ["classification", "severity", "actions"]


def test_injection_canary_case_not_followed(
    pipeline: Any,
    suite: tuple[list[EvalCase], Manifest],
) -> None:
    cases, _ = suite
    case = _case(cases, "injection-01")
    assert "request/password_reset" in case.input["body"]
    assert case.fixture is not None and case.fixture.classification == "request/access"
    run = pipeline.run(case, case.alert(0))
    # The scripted LLM obeyed the injection, the pre-classifier didn't: conflict,
    # safe default, and the injected label is never acted on.
    assert run.decision.reason == "classifier-conflict"
    assert run.acted_label is None
    assert score(case, run).passed


def test_redaction_case_checks_what_reached_the_provider(
    pipeline: Any,
    suite: tuple[list[EvalCase], Manifest],
) -> None:
    cases, _ = suite
    case = _case(cases, "canary-01")
    run = pipeline.run(case, case.alert(0))
    assert run.llm_requests, "the provider saw a request"
    assert score(case, run).passed
    unredacted = CaseRun(
        case_id=run.case_id,
        linked=False,
        decision=run.decision,
        actions=run.actions,
        llm_requests=["caller +14155552671"],
    )
    assert not score(case, unredacted).passed


def _decision(classification: str | None, preclass: str | None) -> TriageDecision:
    return TriageDecision(
        outcome="routed",
        reason="routed",
        classification=classification,
        severity=None,
        preclass_label=preclass,
        calibrated_score=None,
        route_id=None,
        llm_suggestion=None,
    )


def test_calibration_written_with_grade_and_model(
    suite: tuple[list[EvalCase], Manifest],
) -> None:
    cases, _ = suite
    truth = _case(cases, "db-sat-01")  # truth: database/saturation
    other = _case(cases, "ticket-freetext-01")  # truth: request/access

    def run(c: EvalCase, predicted: str | None, preclass: str | None) -> CaseRun:
        return CaseRun(c.id, False, _decision(predicted, preclass), [])

    pairs = [
        (truth, run(truth, "database/saturation", "database/saturation")),  # agree, correct
        (truth, run(truth, "database/latency", "database/saturation")),  # agree, prefix-correct
        (other, run(other, "request/access", None)),  # abstain, correct
        (other, run(other, "auth/failure", None)),  # abstain, wrong
        (truth, run(truth, "network/outage", "database/saturation")),  # conflict: not evidence
        (truth, run(truth, None, "database/saturation")),  # llm-failure: no prediction
    ]
    table = build_calibration(pairs, model_id="claude-sonnet-4-5-20250929", snapshot_id="snap")
    assert (table.grade, table.model_id, table.snapshot_id) == (
        "smoke",
        "claude-sonnet-4-5-20250929",
        "snap",
    )
    sat = table.labels["database/saturation"]
    assert sat.agree is not None and (sat.agree.n, sat.agree.correct) == (1, 1)
    assert sat.abstain is None
    lat = table.labels["database/latency"].agree
    assert lat is not None and (lat.n, lat.correct) == (1, 1)
    req = table.labels["request/access"].abstain
    assert req is not None and (req.n, req.correct) == (1, 1)
    auth = table.labels["auth/failure"].abstain
    assert auth is not None and (auth.n, auth.correct) == (1, 0)
    assert "network/outage" not in table.labels


def test_eval_cli_runs_smoke_offline(tmp_path: Path) -> None:
    out = CliRunner().invoke(
        app,
        [
            "eval",
            "run",
            "--suite",
            "invariants",
            "--manifest",
            str(_MANIFEST),
            "--json",
            "--artifacts",
            str(tmp_path),
        ],
    )
    assert out.exit_code == 0, out.output
    assert '"suite": "invariants"' in out.output
    (report,) = tmp_path.glob("*-invariants-fixture/report.json")
    assert '"snapshot_id": "smoke-v0.0-2026-10-20"' in report.read_text(encoding="utf-8")


def test_eval_cli_refuses_fixture_calibration() -> None:
    out = CliRunner().invoke(app, ["eval", "run", "--suite", "smoke", "--write-calibration"])
    assert out.exit_code == 2
    assert "--live" in out.output


def test_eval_cli_unknown_suite() -> None:
    out = CliRunner().invoke(
        app, ["eval", "run", "--suite", "nonsense", "--manifest", str(_MANIFEST)]
    )
    assert out.exit_code == 2


# --- the spec's test-ID index -------------------------------------------------

_ID = re.compile(r"`(test_[a-z0-9_]+)`")


def _ids(text: str) -> set[str]:
    return set(_ID.findall(text))


def test_spec_test_ids_exist() -> None:
    """Every test ID named by ARCH §19 and the v0.0 WPs is a test function.

    IDs that the v0.1 WP file schedules (its "Tests first" lists) are
    planned, not missing. Templated IDs (``test_doctor_<check>_<status>``)
    are covered by their concrete instances.
    """
    arch = (_REPO / "docs" / "spec" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    section = arch[arch.index("## 19. CI gates") :]
    nxt = section.find("\n## ", 5)
    wanted = _ids(section[: nxt if nxt > 0 else None])
    wanted |= _ids((_REPO / "docs" / "tech-spec" / "10-wp-v0.0.md").read_text(encoding="utf-8"))
    planned = _ids((_REPO / "docs" / "tech-spec" / "11-wp-v0.1.md").read_text(encoding="utf-8"))
    defined: set[str] = set()
    for path in (_REPO / "tests").rglob("*.py"):
        defined |= set(re.findall(r"def (test_[a-z0-9_]+)", path.read_text(encoding="utf-8")))
    missing = sorted(wanted - defined - planned)
    assert not missing, f"spec test IDs with no test: {missing}"
    assert any(t.startswith("test_doctor_") and t.endswith("_pass") for t in defined)


def test_harness_flags_a_fingerprint_collision(
    pipeline: Any,
    suite: tuple[list[EvalCase], Manifest],
) -> None:
    cases, _ = suite
    first = _case(cases, "db-sat-01")
    twin = first.model_copy(update={"id": "db-sat-twin"})  # same alert, same fingerprint
    result = run_suite("smoke", [first, twin], pipeline)
    assert [s.passed for s in result.score.scores] == [True, False]
    assert "collides" in result.score.scores[1].failures[-1]
