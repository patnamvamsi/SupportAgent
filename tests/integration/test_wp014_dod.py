"""WP-014 DoD: a shadow run over 50 fixture incidents reproduces the golden report.

``tests/data/shadow/incidents.jsonl`` is a partner-style export; the scripted
LLM answers each incident from ``llm_script.json`` and spends its scripted
latency on the clock (so alert → first proposal is not trivially 0). The
golden report was computed by ``build_fixture.py`` from each incident's
*intended* outcome, independently of ``StatsService``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from supportagent.adapters.db.repos.cases import CaseRepo
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.repos.shadow import ShadowRepo
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.files.preclass_loader import load_preclass_rules
from supportagent.adapters.llm.client import LLMClient, SlotSpec
from supportagent.adapters.llm.detectors import built_in_detectors
from supportagent.adapters.llm.prompts import PromptRegistry
from supportagent.adapters.llm.redact import Redactor
from supportagent.adapters.servicenow.export import read_export
from supportagent.app.classify import Classifier
from supportagent.app.shadow import ShadowRunner, import_history
from supportagent.app.stats import StatsService
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.calibration import (
    CalibrationCountRow,
    CalibrationLabelRow,
    CalibrationTable,
)
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.routing import Route, RoutingTable
from supportagent.domain.slots import SlotName
from supportagent.ports.llm import ProviderRequest, ProviderResponse
from tests.unit.api_support import service

_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "tests" / "data" / "shadow"
_MODEL = "claude-sonnet-4-5-20250929"
_STRONG = CalibrationCountRow(n=40, correct=40)
_WEAK = CalibrationCountRow(n=5, correct=5)


class _ScriptedLLM:
    """Answers per incident number and spends the scripted latency on the clock."""

    def __init__(self, clock: Any) -> None:
        self._clock = clock
        self._script = json.loads((_DATA / "llm_script.json").read_text(encoding="utf-8"))

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        match = re.search(r"INC\d{7}", request.user)
        assert match, "the incident number reaches the prompt (it is not a secret)"
        entry = self._script[match.group(0)]
        self._clock.advance(entry["latency_s"])
        return ProviderResponse(
            raw_text=entry["raw"],
            resolved_model=_MODEL,
            tokens_in=100,
            tokens_out=30,
            latency_ms=int(entry["latency_s"]) * 1000,
        )


def _calibration() -> CalibrationTable:
    strong = CalibrationLabelRow(agree=_STRONG, abstain=_STRONG)
    labels = dict.fromkeys(
        (
            "database/failure",
            "deploy/failure",
            "network/latency",
            "batch/failure",
            "network/outage",
        ),
        strong,
    )
    labels["auth/failure"] = CalibrationLabelRow(agree=_STRONG, abstain=_WEAK)
    return CalibrationTable(grade="smoke", snapshot_id="snap-dod", model_id=_MODEL, labels=labels)


def _runner(uow: SqlUnitOfWork, clock: Any, ids: Any, aead: Any) -> ShadowRunner:
    events = SqlEventSink(uow=uow, cipher=aead)
    catalog = Catalog(version="1", services=[service("payments"), service("search")])
    llm = LLMClient(
        provider=_ScriptedLLM(clock),
        redactor=Redactor(detectors=built_in_detectors()),
        prompts=PromptRegistry(root=_REPO / "prompts"),
        events=events,
        ids=ids,
        clock=clock,
        slots={SlotName.CLASSIFY: SlotSpec(model_id=_MODEL, timeout_s=15.0, max_output_tokens=400)},
    )
    classifier = Classifier(
        rule_set=load_preclass_rules(_REPO / "taxonomy" / "preclass_rules.yaml"),
        llm=llm,
        calibration=_calibration(),
        min_score=0.80,
        routing=RoutingTable(rules=[], default=Route(id="generic", actions=[ActionKind.WORK_NOTE])),
    )
    return ShadowRunner(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        case_repo=CaseRepo(uow),
        classifier=classifier,
        catalog=catalog,
        repo=ShadowRepo(uow),
    )


def _rounded(value: object) -> object:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    return value


def test_wp014_dod_shadow_run_matches_golden_report(
    db: Any, uow: SqlUnitOfWork, clock: Any, ids: Any, aead: Any
) -> None:
    incidents = list(read_export(_DATA / "incidents.jsonl", tenant_id="acme"))
    assert len(incidents) == 50
    records = import_history(incidents, runner=_runner(uow, clock, ids, aead), clock=clock)
    assert len(records) == 50

    report = StatsService(uow=SqlUnitOfWork(db), clock=clock, tenant_id="acme").report(
        window=None, mode="shadow"
    )
    golden = json.loads((_DATA / "report_golden.json").read_text(encoding="utf-8"))
    assert _rounded(report.as_dict()) == golden

    # Shadow never acts: no proposal, approval or outbox row exists.
    with db.connect() as conn:
        for table in ("proposals", "approvals", "outbox"):
            count = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one()  # noqa: S608
            assert count == 0, table
