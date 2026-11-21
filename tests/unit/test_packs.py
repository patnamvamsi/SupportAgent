"""WP-M01: skill packs load, validate together, and are selected per case (ADR-0011 §1)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypedDict, TypeVar

import pytest

from supportagent.adapters.cel.check import CelCheckCompiler
from supportagent.adapters.files.pack_loader import load_pack_set
from supportagent.adapters.files.runbook_loader import load_runbooks
from supportagent.app.classify import Classifier
from supportagent.app.doctor.base import DoctorContext
from supportagent.app.doctor.checks.local import PacksCheck
from supportagent.app.rca import Knowledge, RcaCase
from supportagent.bootstrap import KNOWN_TOOL_SCHEMAS, _load_bundle, _pack_set
from supportagent.domain.errors import ConfigError
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.base import DomainModel
from supportagent.domain.model.calibration import CalibrationTable
from supportagent.domain.model.case import Case
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.packs import FRAGMENT_BUDGET_CHARS, PackScope, labels_text
from supportagent.domain.routing import Route, RoutingTable
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from tests.builders import an_alert
from tests.unit.test_triage_service import _shadow_config

_REPO = Path(__file__).resolve().parents[2]
TModel = TypeVar("TModel", bound=DomainModel)


class _Legacy(TypedDict):
    taxonomy: Path
    hypotheses: Path
    runbooks: Path


def _legacy(tmp_path: Path) -> _Legacy:
    """The shipped common files, read in place."""
    return {
        "taxonomy": _REPO / "taxonomy" / "classification.yaml",
        "hypotheses": _REPO / "taxonomy" / "hypotheses.yaml",
        "runbooks": _REPO / "runbooks",
    }


def _write_pack(
    root: Path, pack_id: str, manifest: str, files: dict[str, str] | None = None
) -> None:
    directory = root / pack_id
    directory.mkdir(parents=True)
    (directory / "pack.yaml").write_text(manifest, encoding="utf-8")
    for rel, text in (files or {}).items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


_WEB = """\
id: web
version: 1.0.0
description: A test pack for python web services.
applies_to: {stack: [python]}
taxonomy: taxonomy.yaml
hypotheses: hypotheses.yaml
preclass: preclass_rules.yaml
"""
_WEB_FILES = {
    "taxonomy.yaml": (
        "version: 1.0.0\nlabels:\n  - id: app/worker_timeout\n    description: Workers time out.\n"
    ),
    "hypotheses.yaml": "version: 1.0.0\nhypotheses:\n  - id: slow_upstream\n",
    "preclass_rules.yaml": (
        "version: 1.0.0\nrules:\n"
        "  - when: {body: 'WORKER TIMEOUT'}\n    label: app/worker_timeout\n"
    ),
}


def test_pack_manifest_schema_rejects_unknown_keys(tmp_path: Path) -> None:
    _write_pack(tmp_path / "packs", "web", _WEB + "surprise: true\n", _WEB_FILES)
    with pytest.raises(ConfigError, match="surprise"):
        load_pack_set(**_legacy(tmp_path), packs_dir=tmp_path / "packs")


def test_pack_id_must_match_its_directory(tmp_path: Path) -> None:
    _write_pack(tmp_path / "packs", "other", _WEB, _WEB_FILES)
    with pytest.raises(ConfigError, match="must match its directory"):
        load_pack_set(**_legacy(tmp_path), packs_dir=tmp_path / "packs")


def test_common_pack_always_active(tmp_path: Path) -> None:
    packs = load_pack_set(**_legacy(tmp_path))
    assert [p.id for p in packs.active([])] == ["common"]
    assert [p.id for p in packs.active(["anything"])] == ["common"]


def test_pack_selected_by_catalog_stack(tmp_path: Path) -> None:
    _write_pack(tmp_path / "packs", "web", _WEB, _WEB_FILES)
    packs = load_pack_set(**_legacy(tmp_path), packs_dir=tmp_path / "packs")
    assert [p.id for p in packs.active(["python", "redis"])] == ["common", "web"]
    assert [p.id for p in packs.active(["java"])] == ["common"]


def test_pack_ids_unique_across_active_packs(tmp_path: Path) -> None:
    """A label (or hypothesis) defined by two packs is ambiguous: refuse it at load."""
    clash = dict(_WEB_FILES)
    clash["taxonomy.yaml"] = (
        "version: 1.0.0\nlabels:\n  - id: database/saturation\n    description: Duplicate.\n"
    )
    clash["preclass_rules.yaml"] = "version: 1.0.0\nrules: []\n"
    _write_pack(tmp_path / "packs", "web", _WEB, clash)
    with pytest.raises(ConfigError, match="database/saturation"):
        load_pack_set(**_legacy(tmp_path), packs_dir=tmp_path / "packs")


def test_preclass_label_must_exist_in_some_pack(tmp_path: Path) -> None:
    files = dict(_WEB_FILES)
    files["preclass_rules.yaml"] = (
        "version: 1.0.0\nrules:\n  - when: {body: 'x'}\n    label: nowhere/label\n"
    )
    _write_pack(tmp_path / "packs", "web", _WEB, files)
    with pytest.raises(ConfigError, match="nowhere/label"):
        load_pack_set(**_legacy(tmp_path), packs_dir=tmp_path / "packs")


def test_pack_file_cannot_point_outside_the_pack(tmp_path: Path) -> None:
    _write_pack(
        tmp_path / "packs",
        "web",
        _WEB.replace("taxonomy: taxonomy.yaml", "taxonomy: ../../secrets.yaml"),
        _WEB_FILES,
    )
    with pytest.raises(ConfigError, match="outside the pack"):
        load_pack_set(**_legacy(tmp_path), packs_dir=tmp_path / "packs")


def test_prompt_fragments_within_budget(tmp_path: Path) -> None:
    manifest = _WEB + "prompts: {classify: classify.md}\n"
    files = dict(_WEB_FILES, **{"classify.md": "x" * (FRAGMENT_BUDGET_CHARS + 1)})
    _write_pack(tmp_path / "packs", "web", manifest, files)
    with pytest.raises(ConfigError, match="budget"):
        load_pack_set(**_legacy(tmp_path), packs_dir=tmp_path / "packs")


def test_placeholder_pack_loads_not_evaluated(tmp_path: Path) -> None:
    _write_pack(
        tmp_path / "packs",
        "spark",
        "id: spark\nversion: 0.0.0\ndescription: Spark.\nplaceholder: true\n"
        "applies_to: {stack: [spark]}\n",
    )
    packs = load_pack_set(**_legacy(tmp_path), packs_dir=tmp_path / "packs")
    assert packs.get("spark").placeholder
    assert [p.id for p in packs.active(["spark"])] == ["common"], "never selected"


def test_runbook_tools_bound_at_load(tmp_path: Path) -> None:
    """A pack runbook naming a tool the build doesn't know fails when RCA loads it."""
    runbook = (
        "id: runbook:web-timeout\napplies_when: {classification: app/worker_timeout}\n"
        "budget: {max_tool_calls: 2, max_tokens: 500, max_hypotheses: 1}\nsteps:\n"
        "  - id: s1\n    hypothesis: slow_upstream\n    tool: nope.missing\n"
        "    args: {x: 1}\n    check: 'true'\n"
    )
    _write_pack(
        tmp_path / "packs",
        "web",
        _WEB + "runbooks: runbooks/\n",
        dict(_WEB_FILES, **{"runbooks/web-timeout.yaml": runbook}),
    )
    packs = load_pack_set(**_legacy(tmp_path), packs_dir=tmp_path / "packs")
    pack = packs.get("web")
    assert pack.runbook_ids == ("runbook:web-timeout",)
    assert pack.runbooks_dir is not None
    with pytest.raises(ConfigError, match="not registered"):
        load_runbooks(
            Path(pack.runbooks_dir),
            hypotheses=packs.hypothesis_ids(packs.packs),
            tool_schemas=KNOWN_TOOL_SCHEMAS,
            compiler=CelCheckCompiler(),
        )


# --- per-case use -----------------------------------------------------------------


class _LLM:
    def __init__(self) -> None:
        self.variables: dict[str, str] = {}

    def structured(
        self,
        slot: SlotName,
        variables: Mapping[str, str],
        schema: type[TModel],
        trace: TraceContext,
    ) -> TModel | None:
        self.variables = dict(variables)
        result = {"classification": "app/worker_timeout", "severity": "P3"}
        return schema.model_validate_json(json.dumps(result | {"summary": "s", "confidence": 0.9}))


def _classifier(scope: PackScope, llm: _LLM) -> Classifier:
    return Classifier(
        rule_set=scope.packs.rule_set(),
        llm=llm,
        calibration=CalibrationTable(grade="smoke", snapshot_id="s", model_id="m", labels={}),
        min_score=0.0,
        routing=RoutingTable(default=Route(id="generic", actions=[ActionKind.WORK_NOTE])),
        packs=scope,
    )


def test_classifier_sees_only_the_services_packs(tmp_path: Path) -> None:
    _write_pack(tmp_path / "packs", "web", _WEB, _WEB_FILES)
    packs = load_pack_set(**_legacy(tmp_path), packs_dir=tmp_path / "packs")
    scope = PackScope(packs=packs, stacks={"api": frozenset({"python"}), "db": frozenset()})
    evt = AlertEvent.model_validate(an_alert(body="[CRITICAL] WORKER TIMEOUT (pid:12)"))
    trace = TraceContext(tenant_id="acme", trace_id="tr_1", span_id="s", parent_span_id=None)

    llm = _LLM()
    case = Case.model_validate(
        {"id": "c1", "tenant_id": "acme", "fingerprint": "f", "origin": "alert", "service": "api"}
    )
    decision = _classifier(scope, llm).decide(evt, case, trace)
    assert "app/worker_timeout" in llm.variables["taxonomy_labels"]
    assert decision.preclass_label == "app/worker_timeout"

    llm = _LLM()
    other = case.model_copy(update={"service": "db"})
    decision = _classifier(scope, llm).decide(evt, other, trace)
    assert "app/worker_timeout" not in llm.variables["taxonomy_labels"]
    assert decision.preclass_label is None, "the web pack's rule doesn't apply to this service"


def test_rca_knowledge_scoped_to_the_case_packs(tmp_path: Path) -> None:
    _write_pack(tmp_path / "packs", "web", _WEB, _WEB_FILES)
    packs = load_pack_set(**_legacy(tmp_path), packs_dir=tmp_path / "packs")
    kb = Knowledge(
        runbooks=[],
        probes={},
        excludes={h.id: list(h.excludes) for h in packs.hypothesis_taxonomy().hypotheses},
        packs=packs,
    )

    def case(stack: list[str]) -> RcaCase:
        service = ServiceEntry(name="api", stack=stack)
        return RcaCase(
            id="c1",
            tenant_id="acme",
            trace_id="t",
            service=service,
            classification=None,
            severity="P3",
            origin="alert",
            opened_ts=0,
        )

    assert "slow_upstream" in kb.for_case(case(["python"])).hypothesis_ids
    assert "slow_upstream" not in kb.for_case(case(["java"])).hypothesis_ids
    assert "recent_change_push" in kb.for_case(case(["java"])).hypothesis_ids, "common stays"


# --- the shipped repo ----------------------------------------------------------------


def _repo_config() -> Any:
    base = _shadow_config()
    paths = base.paths.model_copy(
        update={
            "packs": str(_REPO / "packs"),
            "catalog": str(_REPO / "catalog" / "services.example.yaml"),
        }
    )
    return base.model_copy(update={"paths": paths})


def test_existing_behaviour_unchanged_with_common_pack() -> None:
    """A service no evaluated pack selects sees exactly the common files; packs only add."""
    config = _repo_config()
    packs = _pack_set(config)
    assert {p.id for p in packs.packs} == {
        "common",
        "data-quality",
        "docker-compose",
        "java-spring",
        "kafka",
        "kubernetes",
        "mongodb",
        "nodejs",
        "oracle",
        "python-postgres",
        "rabbitmq",
        "spark",
        "snowflake",
        "streamlit",
    }
    legacy = load_pack_set(
        taxonomy=Path(config.paths.taxonomy),
        hypotheses=Path(config.paths.hypotheses),
        runbooks=Path(config.paths.runbooks),
    )
    for stack in ([], ["golang"], ["erlang"]):  # no evaluated pack matches: exactly common
        active = packs.active(stack)
        assert labels_text(packs.taxonomy(active).labels) == labels_text(legacy.taxonomy().labels)
        assert packs.rule_set(active) == legacy.rule_set()
    web = packs.active(["python", "postgres"])  # WP-M03: the pack adds to common, never edits it
    assert [p.id for p in web] == ["common", "python-postgres"]
    common_ids = [label.id for label in legacy.taxonomy().labels]
    assert [label.id for label in packs.taxonomy(web).labels][: len(common_ids)] == common_ids
    assert packs.rule_set(web).rules[: len(legacy.rule_set().rules)] == legacy.rule_set().rules


def test_doctor_reports_packs_and_their_eval_status() -> None:
    config = _repo_config()
    bundle = _load_bundle(config)
    api = ServiceEntry(name="checkout-api", stack=["python", "postgres"])
    catalog = bundle.catalog.model_copy(update={"services": [*bundle.catalog.services, api]})

    ctx: Any = DoctorContext.__new__(DoctorContext)
    object.__setattr__(ctx, "config", config)
    object.__setattr__(ctx, "load_bundle", lambda: dataclasses.replace(bundle, catalog=catalog))
    object.__setattr__(ctx, "load_packs", lambda: _pack_set(config))
    object.__setattr__(ctx, "known_tools", frozenset(KNOWN_TOOL_SCHEMAS))
    res = PacksCheck().run(ctx)
    assert res.status == "pass", res.detail
    assert "python-postgres 1.2.0: evaluated; used by checkout-api" in res.detail
    assert "spark 0.1.0: evaluated; used by no service" in res.detail  # System B's knowledge
    assert "tools not available" not in res.detail, "WP-M04 ships every tool the pack names"
