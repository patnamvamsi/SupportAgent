"""ADR-0017 (WP-A6): ``doctor``'s onboarding check, and the facts the wiring gives it."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.app.doctor import DoctorContext
from supportagent.app.doctor.base import OnboardingFacts
from supportagent.app.doctor.checks.local import OnboardingCheck
from supportagent.app.readiness_store import ReadinessStore
from supportagent.bootstrap import _calibration_in_use, onboarding_facts
from supportagent.config.loader import load_config_offline
from supportagent.domain.errors import ConfigError
from supportagent.domain.model.calibration import CalibrationTable

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_FACTS = OnboardingFacts(
    plugins=[("oracle", "1.0.0")],
    disabled=[],
    catalogs=[("/srv/catalog", 3, "")],
    unserved=[],
    readiness_present=True,
    readiness_at="2026-11-02T09:30:00+00:00",
    plugins_changed=False,
    routing="enforce",
    calibration=("fitted", "fitted with qwen"),
)


def _run(facts: OnboardingFacts | Exception) -> Any:
    def load() -> OnboardingFacts:
        if isinstance(facts, Exception):
            raise facts
        return facts

    ctx = DoctorContext.__new__(DoctorContext)
    object.__setattr__(ctx, "load_onboarding", load)
    return OnboardingCheck().run(ctx)


def test_onboarding_pass_names_what_it_runs_with() -> None:
    res = _run(_FACTS)
    assert res.status == "pass", res.detail
    for part in ("1 plugin(s)", "3 plugin(s)", "2026-11-02 09:30", "routing: enforce", "fitted"):
        assert part in res.detail


def test_onboarding_warns_on_an_unserved_capability() -> None:
    res = _run(dataclasses.replace(_FACTS, unserved=[("mongodb.current_op", "mongodb")]))
    assert res.status == "warn"
    assert "mongodb.current_op (declared by mongodb) has no MCP server bound" in res.detail
    assert res.data["unserved"] == [["mongodb.current_op", "mongodb"]]


def test_onboarding_warns_when_plugins_changed_or_a_catalogue_fails() -> None:
    res = _run(
        dataclasses.replace(
            _FACTS, plugins_changed=True, catalogs=[("/srv/gone", None, "no index.yaml")]
        )
    )
    assert res.status == "warn"
    assert "plugins changed since readiness" in res.detail
    assert "catalogue /srv/gone doesn't load: no index.yaml" in res.detail


def test_onboarding_without_a_report_or_disabled_plugins() -> None:
    res = _run(
        dataclasses.replace(_FACTS, readiness_present=False, readiness_at=None, disabled=["kafka"])
    )
    assert res.status == "pass"
    assert "not assessed" in res.detail and "disabled: kafka" in res.detail


def test_onboarding_fails_when_the_plugins_dont_load() -> None:
    res = _run(ConfigError("plugin x: manifest changed since it was pinned"))
    assert res.status == "fail" and "manifest changed" in res.detail


def _config(**onboarding: str) -> Any:
    cfg = load_config_offline(_REPO / "evals/lab/lab.yaml")
    return cfg.model_copy(update={"onboarding": cfg.onboarding.model_copy(update=onboarding)})


def _table(model: str) -> CalibrationTable:
    table = CalibrationTable(grade="smoke", snapshot_id="s", model_id=model, labels={})
    return table.model_copy(update={"signature": table.computed_signature()})


def test_calibration_in_use_says_why() -> None:
    cfg = _config()
    model = cfg.llm.slots["classify"].model_id
    fitted = {"calibration": _table(model).model_dump(mode="json")}
    assert _calibration_in_use(cfg, None) == (None, "nothing fitted yet")
    table, why = _calibration_in_use(cfg, fitted)
    assert table is not None and why == f"fitted with {model}"
    other = {"calibration": _table("other-model").model_dump(mode="json")}
    assert _calibration_in_use(cfg, other)[1] == f"fitted with other-model, triage uses {model}"
    edited = dict(fitted["calibration"], snapshot_id="edited")
    assert "edited" in _calibration_in_use(cfg, {"calibration": edited})[1]
    switched = _calibration_in_use(_config(calibration="artifact"), fitted)
    assert switched == (None, "onboarding.calibration: artifact")


def test_onboarding_facts_from_the_deployment(db: Any, clock: Any) -> None:
    cfg = _config(routing="report")
    facts = onboarding_facts(cfg, db, clock)
    assert facts.plugins and not facts.readiness_present and facts.plugins_changed is None
    assert facts.routing == "report"
    # mongodb and rabbitmq declare capabilities their packs check; nothing binds them here
    assert ("rabbitmq.queues", "rabbitmq") in facts.unserved
    ReadinessStore(uow=SqlUnitOfWork(db), clock=clock, tenant_id=cfg.tenant.id).save(
        {"v": 1, "classes": [], "at": "2026-11-02T09:30:00+00:00", "plugins_index": "stale"}
    )
    facts = onboarding_facts(cfg, db, clock)
    assert facts.readiness_present and facts.readiness_at == "2026-11-02T09:30:00+00:00"
    assert facts.plugins_changed is True
