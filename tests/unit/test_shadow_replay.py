"""WP-O6c: shadow replay of a system's history, and classes proven live.

The lab proves the agent can find each class's cause. Shadow replay asks the
next question: would the pipeline (dedup, grouping, calibration, the severity
cap, routing by readiness) have let it act? The model stand-in classifies
every ticket as the truth says, so what's under test is the pipeline.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from supportagent.app.live_proof import LiveRca
from supportagent.bootstrap import (
    ONBOARDING_DIR,
    LabPipeline,
    OnboardingRun,
    _readiness_needs,
    build_readiness,
    build_system_graph,
)
from supportagent.config.loader import load_config_offline
from supportagent.domain.classification import calibrated_score
from supportagent.domain.model.readiness import ReadinessGates
from supportagent.evals.readiness import (
    ReadinessReport,
    Replayed,
    ShadowCase,
    apply_shadow,
    assess,
    fit_calibration,
    observations_needed,
    regressions,
)
from tests.unit.test_readiness import _LAB, _Classifier, _score, _truth_map

pytestmark = pytest.mark.unit

_CLASS = "oracle-tablespace-full"
_LABEL = "database/failure"  # the tablespace class's label


def _lab_ready() -> ReadinessReport:
    """System A's map with only the tablespace class replayed, and ready."""
    imap = _truth_map("system-a")
    members = next(c for c in imap.classes if c.id == _CLASS).incidents
    report = assess(
        imap,
        {_CLASS: [Replayed(n, "oracle_tablespace_full", _score(ok=True)) for n in members]},
        {},
        testable=lambda _: None,
        gates=ReadinessGates(),
        system="payments-platform",
        model_id="m",
    )
    report.calibration = fit_calibration(
        [
            replace(r, predicted=_LABEL, preclass=None)
            for c in report.classes
            if c.class_id == _CLASS
            for r in [Replayed(n, "x", _score(ok=True)) for n in members]
        ],
        model_id="m",
        system="payments-platform",
    )
    return report


def _members() -> tuple[str, ...]:
    return next(c for c in _truth_map("system-a").classes if c.id == _CLASS).incidents


def test_a_class_shadow_routes_with_its_label_stays_ready() -> None:
    report = _lab_ready()
    cases = [ShadowCase(n, "routed", "routed", _LABEL, route="db-saturation") for n in _members()]
    out = apply_shadow(report, _truth_map("system-a"), cases, gates=ReadinessGates(), min_score=0.8)
    cls = next(c for c in out.classes if c.class_id == _CLASS)
    assert cls.status == "ready" and cls.lab_status == "ready"
    assert cls.routed == len(_members())
    assert out.not_measured == ()
    shown = next(c for c in out.as_dict()["classes"] if c["id"] == _CLASS)
    assert shown["routed"] == len(_members()) and len(shown["shadow"]) == len(_members())


def test_shadow_holds_back_a_lab_ready_class_and_says_what_it_waits_for() -> None:
    report = _lab_ready()
    first, *rest = _members()
    cases = [
        ShadowCase(first, "routed", "routed", _LABEL),
        *(ShadowCase(n, "safe_default", "low-calibrated-score", _LABEL) for n in rest[:-1]),
        # dedup put the last one on the first one's case: never triaged on its own
        ShadowCase(rest[-1], "routed", "routed", _LABEL, merged_into=first),
    ]
    out = apply_shadow(
        report,
        _truth_map("system-a"),
        cases,
        gates=ReadinessGates(),
        min_score=0.8,
        catalog_gaps=[("payments-api", "not in the catalog (CMDB CI payments-api): add it")],
    )
    cls = next(c for c in out.classes if c.class_id == _CLASS)
    assert cls.status == "not_ready" and cls.lab_status == "ready"
    n = len(_members())
    why = cls.reasons[-1]
    assert why.startswith(f"shadow routed 1/{n}, below the 75% needed")
    assert "1 merged into an earlier case" in why
    assert f"{n - 2} low-calibrated-score" in why
    # n observations so far, all correct: ADR-0002 needs 10 and a Wilson bound of 0.80
    assert f"needs {observations_needed(n, n, 0.8)} more correctly classified" in why
    # the lab baseline guards the lab's verdict; the routing verdict is shadow's
    assert regressions(out, {_CLASS: "ready"}) == []
    assert out.ready_share == 0
    needs = _readiness_needs(out.as_dict())
    assert any(need.kind == "shadow" and "oracle/tablespace_full" in need.what for need in needs)
    assert [need.what for need in needs if need.kind == "config"] == [
        "catalog: payments-api: not in the catalog (CMDB CI payments-api): add it"
    ]
    assert "  catalog: payments-api: not in the catalog (CMDB CI payments-api): add it" in (
        out.lines()
    )


def test_how_many_more_correct_observations_clear_the_floor() -> None:
    assert observations_needed(0, 0, 0.8) == 16  # Wilson(16/16) = 0.806
    assert observations_needed(5, 5, 0.8) == 11
    assert observations_needed(20, 20, 0.8) == 0
    assert observations_needed(10, 9, 0.0) == 0  # n >= 10 is enough with no floor
    assert observations_needed(3, 3, 0.0) == 7  # ... but n >= 10 always holds
    assert observations_needed(0, 0, 1.0) is None


def test_resolved_live_cases_grow_the_fitted_calibration() -> None:
    table = fit_calibration(
        [Replayed("I1", "x", _score(ok=True), predicted=_LABEL, preclass=None)],
        model_id="m",
        system="s",
        observed=[
            (_LABEL, None, True),
            (_LABEL, None, False),
            (_LABEL, "database/latency", True),  # the pre-classifier agreed (same domain)
            (_LABEL, "network/outage", True),  # a conflict never routes: not evidence
            (None, None, True),
        ],
    )
    row = table.labels[_LABEL]
    assert row.abstain is not None and (row.abstain.n, row.abstain.correct) == (3, 2)
    assert row.agree is not None and (row.agree.n, row.agree.correct) == (1, 1)


def test_enough_correct_live_cases_lift_a_label_over_the_floor() -> None:
    """The way out ADR-0020 chose: the floor stays, and live evidence reaches it."""
    table = fit_calibration([], model_id="m", system="s", observed=[(_LABEL, None, True)] * 15)
    assert calibrated_score(table, _LABEL, preclass_agrees=False) < 0.8
    table = fit_calibration([], model_id="m", system="s", observed=[(_LABEL, None, True)] * 16)
    assert calibrated_score(table, _LABEL, preclass_agrees=False) >= 0.8


def _system_a(**update: object) -> tuple[ReadinessReport, ReadinessReport]:
    """System A's readiness without and with shadow replay."""
    d = ONBOARDING_DIR / "system-a"
    cfg = load_config_offline(_LAB)
    if update:
        cfg = cfg.model_copy(update={"calibration": cfg.calibration.model_copy(update=update)})
    run = OnboardingRun(
        None,  # type: ignore[arg-type] # the result and plan aren't read
        [],
        build_system_graph(d, None),
        _truth_map("system-a"),
        None,  # type: ignore[arg-type]
    )
    lab = LabPipeline(cfg, provider=_Classifier("system-a"))
    return build_readiness(d, run, lab, cfg), build_readiness(d, run, lab, cfg, shadow=lab.shadow())


def test_system_a_history_is_too_small_to_route_on_its_own_calibration() -> None:
    """The finding shadow replay exists for: the lab proves classes the pipeline won't route."""
    lab, shadow = _system_a()
    assert lab.ready and lab.not_measured == ("shadow replay",)
    assert shadow.not_measured == ()
    assert shadow.ready_share == 0 and not shadow.ready
    assert regressions(shadow, lab.statuses()) == []
    for cls in shadow.classes:
        if cls.lab_status != "ready":
            continue
        assert cls.status == "not_ready", cls.class_id
        assert cls.shadow is not None and len(cls.shadow) >= cls.scenarios
        assert "learning live" in cls.reasons[-1] or "low-calibrated-score" in cls.reasons[-1]


def test_system_a_routes_in_shadow_once_calibration_doesnt_hold_it_back() -> None:
    _, shadow = _system_a(min_score=0.0)
    routed = {
        c.class_id: c for c in shadow.classes if c.lab_status == "ready" and c.status == "ready"
    }
    assert routed, shadow.lines()
    for cls in routed.values():
        assert cls.shadow is not None
        assert all(not s.merged_into for s in cls.shadow if s.routes(cls.label or ""))
    # a P1/P2 ticket without a pre-classifier label still goes to a person (ARCH §4 step 5)
    capped = [
        s for c in shadow.classes for s in c.shadow or () if s.reason == "unconfirmed-high-severity"
    ]
    assert all(s.outcome == "safe_default" for s in capped)


def test_an_incident_without_evidence_is_proven_by_the_rca_shadow_ran_live() -> None:
    """System C's upstream outages left no evidence to replay; their live RCA proves them."""
    d = ONBOARDING_DIR / "system-c"
    cfg = load_config_offline(_LAB)
    imap = _truth_map("system-c")
    run = OnboardingRun(None, [], build_system_graph(d, None), imap, None)  # type: ignore[arg-type]
    lab = LabPipeline(cfg, provider=_Classifier("system-c"))
    before = {c.class_id: c for c in build_readiness(d, run, lab, cfg).classes}
    cls = before["upstream-provider"]
    assert cls.no_evidence and cls.status == "not_ready"
    live = {
        n: LiveRca(
            ticket=n,
            verdict="SUPPORTED",
            supported=cls.hypothesis,
            tested=(str(cls.hypothesis),),
            classification=cls.label,
            preclass=None,
            severity="P3",
        )
        for n in cls.no_evidence
    }
    after = next(
        c
        for c in build_readiness(d, run, lab, cfg, live=live).classes
        if c.class_id == cls.class_id
    )
    assert after.proven_live == cls.no_evidence
    assert after.no_evidence == ()
    assert after.scenarios == cls.scenarios + len(cls.no_evidence)
    assert after.correct == cls.correct + len(cls.no_evidence)
    shown = after.outcomes[-len(cls.no_evidence) :]
    assert all(ok for _, _, ok in shown)
