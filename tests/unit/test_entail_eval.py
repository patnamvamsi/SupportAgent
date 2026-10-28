"""BL-039: the citation-faithfulness suite scores the ``entail`` checker, failing closed."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from supportagent.app.rca import entail_variables
from supportagent.bootstrap import load_entail_suite
from supportagent.config.loader import load_config_offline
from supportagent.evals.entail import EntailCase, EntailGates, Verdict, majority, run_entail

_REPO = Path(__file__).resolve().parents[2]


def _case(expected: str, case_id: str = "pair-one") -> EntailCase:
    return EntailCase.model_validate(
        {
            "id": case_id,
            "claim": "pg_table_bloat: double(result.max_dead_ratio) > 0.5 -> true",
            "evidence": [{"max_dead_ratio": 0.69}],
            "expected": expected,
            "why": "w",
        }
    )


def test_majority_ties_fail_closed() -> None:
    assert majority(["supports", "supports", "unknown"]) == "supports"
    assert majority(["supports", "contradicts"]) == "unknown"


def test_one_false_support_in_any_sample_fails_the_gate() -> None:
    answers: Iterator[Verdict] = iter(["contradicts", "supports", "contradicts"])
    report = run_entail(
        [_case("contradicts")],
        lambda _c, _n: next(answers),
        mode="t",
        model_id="m",
        samples=3,
        gates=EntailGates(min_cases=1),
    )
    [score] = report.scores
    assert score.ok and score.false_support  # right by majority, unsafe in one sample
    assert not report.passed
    assert next(g for g in report.gates if g.name == "false_support").ok is False


def test_no_answer_counts_as_unknown() -> None:
    report = run_entail(
        [_case("unknown"), _case("supports", "pair-two")],
        lambda _c, _n: None,
        mode="t",
        model_id="m",
        gates=EntailGates(min_cases=1, min_faithfulness=0.5),
    )
    assert [s.got for s in report.scores] == ["unknown", "unknown"]
    assert report.faithfulness == 0.5 and report.passed


def test_suite_is_curated_across_verdicts_and_sent_as_rca_sends_it() -> None:
    config = load_config_offline(_REPO / "supportagent.example.yaml")
    cases, gates = load_entail_suite(config)
    counts = {
        v: sum(c.expected == v for c in cases) for v in ("supports", "contradicts", "unknown")
    }
    assert len(cases) >= gates.min_cases and min(counts.values()) >= 5
    assert sum("injection" in c.tags for c in cases) >= 2
    assert all(c.claim.endswith("-> true") for c in cases)
    assert gates.min_faithfulness == 0.95 and gates.max_false_support == 0
    # The request is RCA's: the same variables, so the suite tests what production sends.
    case = cases[0]
    variables = entail_variables(case.claim, case.evidence)
    assert variables["claim"] == case.claim and variables["evidence"].startswith("[{")
