"""WP-110: RCA-lite (ARCH §5, §5.1, §5.4, §5.5).

Canonical test IDs from 11-wp §WP-110 that live here:
``test_reduce_truth_table``, ``test_rca_no_runbook_no_probes_inconclusive``,
``test_rca_runbook_steps_in_order``, ``test_rca_budget_exhaustion_inconclusive``,
``test_rca_stop_if_short_circuits``, ``test_cel_compile_fails_on_unknown_field``,
``test_cel_never_executes_python``, ``test_runbook_customer_comms_rejected``,
``test_entail_failure_tags_speculation``, ``test_contradicted_claim_flagged_not_sent``,
``test_unregistered_tool_hypothesis_never_supported``, ``test_freeze_zeroes_change_prior``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import text

from supportagent.adapters.cel.check import CelCheckCompiler
from supportagent.adapters.fakes.clock import FrozenClock
from supportagent.adapters.fakes.events import RecordingEventSink
from supportagent.adapters.fakes.ids import SequentialIdGenerator
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.files.runbook_loader import load_probes, load_runbooks
from supportagent.adapters.tools.airflow import SCHEMA as AIRFLOW_SCHEMA
from supportagent.adapters.tools.gha import SCHEMA as GHA_SCHEMA
from supportagent.adapters.tools.logs import SCHEMA as LOGS_SCHEMA
from supportagent.app.rca import Budget, Knowledge, RcaCase, RcaService
from supportagent.domain.errors import ConfigError, ToolError
from supportagent.domain.events import EventKind
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.rca_reduce import (
    Claim,
    StepResult,
    apply_verdict,
    order_by_prior,
    reduce,
)
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from supportagent.ports.tools import ToolResult
from tests.unit.api_support import ADA, catalog, login, make_client, seed_proposal

ROOT = Path(__file__).resolve().parents[2]
HYPOTHESES = [
    "recent_change_push",
    "capacity_decay",
    "dependency_failure",
    "upstream_provider",
    "traffic_spike",
    "data_volume",
]
EXCLUDES: dict[str, list[str]] = {h: [] for h in HYPOTHESES} | {
    "capacity_decay": ["recent_change_push"]
}
SCHEMAS: dict[str, Mapping[str, object]] = {
    "gha.recent_runs": GHA_SCHEMA,
    "logs.grep": LOGS_SCHEMA,
    "airflow.dag_runs": AIRFLOW_SCHEMA,
}
SERVICE = ServiceEntry(
    name="payroll-api",
    repos=["github:acme/payroll-api"],
    log_paths=["payroll-api"],
    dags=["nightly-sync"],
)
COMPILER = CelCheckCompiler()

FAILED_RUN = {
    "repo": "acme/payroll-api",
    "runs": [{"id": 1, "status": "completed", "conclusion": "failure"}],
    "failed_jobs": [{"name": "deploy"}],
}
GREEN_RUN = {
    "repo": "acme/payroll-api",
    "runs": [{"id": 2, "status": "completed", "conclusion": "success"}],
    "failed_jobs": [],
}
POOL_LOGS = {
    "path": "payroll-api",
    "count": 2,
    "matches": [{"file": "app.log", "line_no": 3, "text": "pool exhausted"}],
}
QUIET_LOGS = {"path": "payroll-api", "count": 0, "matches": []}
DAG_OK = {"dag_id": "nightly-sync", "runs": [], "failed_tasks": []}


class FakeTools:
    """Registry stand-in: canned results (or errors) per tool; records call order."""

    def __init__(self, results: Mapping[str, Mapping[str, object] | Exception]) -> None:
        self._results = dict(results)
        self.calls: list[str] = []

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._results)

    def call(
        self,
        name: str,
        template: Mapping[str, object],
        service: ServiceEntry,
        trace: TraceContext,
    ) -> ToolResult:
        self.calls.append(name)
        out = self._results[name]
        if isinstance(out, Exception):
            raise out
        return ToolResult(out)


class ScriptedLLM:
    """``StructuredLLM`` stand-in: a function per slot; ``None`` means the slot failed."""

    def __init__(self, script: Mapping[SlotName, Callable[[Mapping[str, str]], object]]) -> None:
        self._script = dict(script)
        self.calls: list[SlotName] = []

    def structured(
        self, slot: SlotName, variables: Mapping[str, str], schema: type[Any], trace: Any
    ) -> Any:
        self.calls.append(slot)
        fn = self._script.get(slot)
        raw = fn(variables) if fn else None
        if raw is None:
            return None
        assert issubclass(schema, BaseModel)
        return schema.model_validate(raw)


def _supports(_: Mapping[str, str]) -> object:
    return {"verdict": "supports"}


def _case(**overrides: Any) -> RcaCase:
    values: dict[str, Any] = {
        "id": "case_1",
        "tenant_id": "acme",
        "trace_id": "tr_rca",
        "service": SERVICE,
        "classification": "database/saturation",
        "severity": "P2",
        "origin": "alert",
        "opened_ts": 1_700_000_000,
    }
    values.update(overrides)
    return RcaCase(**values)


def _knowledge(
    runbooks_dir: Path | None = None, *, registered: frozenset[str] | None = None
) -> Knowledge:
    runbooks = (
        load_runbooks(runbooks_dir, hypotheses=HYPOTHESES, tool_schemas=SCHEMAS, compiler=COMPILER)
        if runbooks_dir
        else []
    )
    known = registered if registered is not None else frozenset(SCHEMAS)
    probes = load_probes(
        ROOT / "runbooks" / "probes.yaml",
        hypotheses=HYPOTHESES,
        tool_schemas={k: v for k, v in SCHEMAS.items() if k in known},
        compiler=COMPILER,
    )
    return Knowledge(runbooks=runbooks, probes=probes, excludes=EXCLUDES)


def _service(  # noqa: PLR0913 — the service ports plus the freeze flag
    knowledge: Knowledge,
    tools: FakeTools,
    llm: ScriptedLLM,
    *,
    events: RecordingEventSink | None = None,
    freeze: bool = False,
    clock: FrozenClock | None = None,
) -> RcaService:
    return RcaService(
        knowledge=knowledge,
        tools=tools,
        llm=llm,
        events=events if events is not None else RecordingEventSink(),
        ids=SequentialIdGenerator(),
        clock=clock or FrozenClock(start=datetime(2026, 10, 21, tzinfo=UTC)),
        prior={"recent_change_push": 0.35, "capacity_decay": 0.2},
        freeze=freeze,
    )


def _write_runbook(directory: Path, body: str, name: str = "rb.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")
    return directory


RUNBOOK = """\
id: runbook:pool
applies_when: {classification: database/saturation}
steps:
  - id: s1
    hypothesis: recent_change_push
    tool: gha.recent_runs
    args: {repo: "$service.repos[0]"}
    check: "result.runs.exists(r, r.conclusion == 'failure')"
    stop_if: %(stop)s
  - id: s2
    hypothesis: dependency_failure
    tool: airflow.dag_runs
    args: {dag_id: "$service.dags[0]"}
    check: "size(result.failed_tasks) > 0"
"""


# -- reduce() -----------------------------------------------------------------


def _r(step: str, hyp: str, *, ran: bool = True, ok: bool = False) -> StepResult:
    return StepResult(step, hyp, ran=ran, ok=ok, why="w")


def test_reduce_truth_table() -> None:
    # Row 1: two ok steps whose hypotheses exclude each other -> INCONCLUSIVE (conflict).
    conflict = reduce(
        [_r("s1", "recent_change_push", ok=True), _r("s2", "capacity_decay", ok=True)],
        plan_length=2,
        excludes=EXCLUDES,
    )
    assert conflict.outcome == "INCONCLUSIVE"
    assert "conflicting" in conflict.reason
    # Row 2: >= 1 ok step, no conflict -> SUPPORTED by the first in plan order.
    supported = reduce(
        [_r("s1", "dependency_failure"), _r("s2", "traffic_spike", ok=True)],
        plan_length=3,
        excludes=EXCLUDES,
    )
    assert (supported.outcome, supported.supported_by) == ("SUPPORTED", "s2")
    first = reduce(
        [_r("s1", "dependency_failure", ok=True), _r("s2", "traffic_spike", ok=True)],
        plan_length=2,
        excludes=EXCLUDES,
    )
    assert first.supported_by == "s1"
    # Row 3: plan completed, every step ran, none ok -> CONSISTENT_UNCONFIRMED.
    unconfirmed = reduce(
        [_r("s1", "dependency_failure"), _r("s2", "traffic_spike")],
        plan_length=2,
        excludes=EXCLUDES,
    )
    assert unconfirmed.outcome == "CONSISTENT_UNCONFIRMED"
    # Row 4: anything else -> INCONCLUSIVE: a failed tool call, an incomplete plan, no plan.
    failed = reduce(
        [_r("s1", "dependency_failure"), _r("s2", "traffic_spike", ran=False)],
        plan_length=2,
        excludes=EXCLUDES,
    )
    assert failed.outcome == "INCONCLUSIVE"
    assert "s2" in failed.reason
    incomplete = reduce([_r("s1", "dependency_failure")], plan_length=3, excludes=EXCLUDES)
    assert incomplete.outcome == "INCONCLUSIVE"
    assert reduce([], plan_length=0, excludes=EXCLUDES).outcome == "INCONCLUSIVE"
    # An ok step that didn't run never counts.
    assert (
        reduce(
            [_r("s1", "dependency_failure", ran=False, ok=True)],
            plan_length=1,
            excludes=EXCLUDES,
        ).outcome
        == "INCONCLUSIVE"
    )


# -- planning and steps -------------------------------------------------------


def test_rca_no_runbook_no_probes_inconclusive() -> None:
    # No runbook matches, and the LLM proposes only hypotheses without a probe.
    llm = ScriptedLLM(
        {SlotName.PROPOSE_HYPOTHESES: lambda _: {"hypotheses": ["traffic_spike", "data_volume"]}}
    )
    events = RecordingEventSink()
    tools = FakeTools({"gha.recent_runs": FAILED_RUN})
    artifact = _service(_knowledge(), tools, llm, events=events).run(
        _case(classification="network/dns"), Budget()
    )
    assert artifact.outcome == "INCONCLUSIVE"
    assert artifact.reason == "no testable plan"
    assert tools.calls == []
    # Ids outside the taxonomy are dropped too, never invented.
    llm2 = ScriptedLLM({SlotName.PROPOSE_HYPOTHESES: lambda _: {"hypotheses": ["made_up"]}})
    assert _service(_knowledge(), tools, llm2).run(_case(), Budget()).plan == "none"
    reasons = [e.payload.get("reason") for e in events.appended if e.kind is EventKind.DECISION]
    assert "no_runbook_no_probes" in reasons


def test_rca_runbook_steps_in_order(tmp_path: Path) -> None:
    rb_dir = _write_runbook(tmp_path / "rb", RUNBOOK % {"stop": "false"})
    tools = FakeTools({"gha.recent_runs": GREEN_RUN, "airflow.dag_runs": DAG_OK})
    llm = ScriptedLLM({})
    events = RecordingEventSink()
    artifact = _service(_knowledge(rb_dir), tools, llm, events=events).run(_case(), Budget())
    assert tools.calls == ["gha.recent_runs", "airflow.dag_runs"]
    assert [e.step.step_id for e in artifact.evidence] == ["s1", "s2"]
    assert artifact.plan == "runbook:pool"
    assert artifact.outcome == "CONSISTENT_UNCONFIRMED"
    # The runbook is the plan: the LLM is never asked to propose hypotheses.
    assert SlotName.PROPOSE_HYPOTHESES not in llm.calls
    steps = [e.payload["step"] for e in events.appended if e.kind is EventKind.RCA_STEP]
    assert steps == ["s1", "s2"]


def test_rca_shipped_db_pool_runbook_supported() -> None:
    tools = FakeTools({"gha.recent_runs": FAILED_RUN, "logs.grep": POOL_LOGS})
    llm = ScriptedLLM({SlotName.ENTAIL: _supports})
    artifact = _service(_knowledge(ROOT / "runbooks"), tools, llm).run(_case(), Budget())
    assert artifact.plan == "runbook:db-pool"
    assert artifact.outcome == "SUPPORTED"
    assert artifact.supported_by == "s1"
    assert tools.calls == ["gha.recent_runs"]  # s1 is stop_if
    assert artifact.comms_template == "db-pool-internal"
    assert artifact.as_dict()["comms_if_supported"] == {
        "audience": ["internal"],
        "template": "db-pool-internal",
    }


def test_rca_budget_exhaustion_inconclusive(tmp_path: Path) -> None:
    rb_dir = _write_runbook(tmp_path / "rb", RUNBOOK % {"stop": "false"})
    tools = FakeTools({"gha.recent_runs": GREEN_RUN, "airflow.dag_runs": DAG_OK})
    events = RecordingEventSink()
    budget = Budget(max_tool_calls=1)
    artifact = _service(_knowledge(rb_dir), tools, ScriptedLLM({}), events=events).run(
        _case(), budget
    )
    assert tools.calls == ["gha.recent_runs"]
    assert artifact.outcome == "INCONCLUSIVE"
    assert "incomplete" in artifact.reason
    assert artifact.budget == {"tool_calls": 1, "tokens": 0}
    reasons = [e.payload.get("reason") for e in events.appended if e.kind is EventKind.DECISION]
    assert "budget_stop" in reasons  # exhaustion is logged


def test_rca_stop_if_short_circuits(tmp_path: Path) -> None:
    rb_dir = _write_runbook(tmp_path / "rb", RUNBOOK % {"stop": "true"})
    tools = FakeTools({"gha.recent_runs": FAILED_RUN, "airflow.dag_runs": DAG_OK})
    llm = ScriptedLLM({SlotName.ENTAIL: _supports})
    artifact = _service(_knowledge(rb_dir), tools, llm).run(_case(), Budget())
    assert tools.calls == ["gha.recent_runs"]
    assert artifact.outcome == "SUPPORTED"
    # Without the stop, the plan would have continued.
    rb2 = _write_runbook(tmp_path / "rb2", RUNBOOK % {"stop": "false"})
    tools2 = FakeTools({"gha.recent_runs": FAILED_RUN, "airflow.dag_runs": DAG_OK})
    _service(_knowledge(rb2), tools2, llm).run(_case(), Budget())
    assert tools2.calls == ["gha.recent_runs", "airflow.dag_runs"]


def test_rca_proposed_plan_uses_registered_probes_by_prior() -> None:
    llm = ScriptedLLM(
        {
            SlotName.PROPOSE_HYPOTHESES: lambda _: {
                "hypotheses": ["dependency_failure", "capacity_decay", "recent_change_push"]
            },
            SlotName.ENTAIL: _supports,
        }
    )
    tools = FakeTools(
        {"gha.recent_runs": GREEN_RUN, "logs.grep": QUIET_LOGS, "airflow.dag_runs": DAG_OK}
    )
    artifact = _service(_knowledge(), tools, llm).run(_case(classification="network/dns"), Budget())
    # Ordered by the prior (0.35, 0.2, 0), each tested with its registered probe.
    assert tools.calls == ["gha.recent_runs", "logs.grep", "airflow.dag_runs"]
    assert artifact.plan == "proposed"
    assert artifact.outcome == "CONSISTENT_UNCONFIRMED"


def test_rca_tool_failure_inconclusive(tmp_path: Path) -> None:
    rb_dir = _write_runbook(tmp_path / "rb", RUNBOOK % {"stop": "false"})
    tools = FakeTools({"gha.recent_runs": ToolError("503"), "airflow.dag_runs": DAG_OK})
    artifact = _service(_knowledge(rb_dir), tools, ScriptedLLM({})).run(_case(), Budget())
    assert artifact.outcome == "INCONCLUSIVE"
    assert not artifact.evidence[0].step.ran


# -- CEL checks ---------------------------------------------------------------


def test_cel_compile_fails_on_unknown_field() -> None:
    for expr in (
        "result.nope > 0",
        "result.matches.exists(m, m.colour == 'red')",
        "service.not_a_catalog_field == 'x'",
        "case.subject == 'x'",
        "size(result.runs) > 0",  # a gha field against the logs schema
        "result.count >",  # not valid CEL
    ):
        with pytest.raises(ConfigError):
            COMPILER.compile(expr, LOGS_SCHEMA)
    # Known fields compile.
    COMPILER.compile("result.count > 0 && service.name != '' && case.severity == 'P1'", LOGS_SCHEMA)


def test_cel_never_executes_python(tmp_path: Path) -> None:
    for expr in (
        "__import__('os').system('touch pwned')",
        "os.system('touch pwned')",
        "result.__class__.__mro__",
        "exec('1')",
        "open('/etc/passwd')",
    ):
        with pytest.raises(ConfigError):
            COMPILER.compile(expr, LOGS_SCHEMA)
    # Data that looks like Python is only ever data.
    check = COMPILER.compile("result.matches.exists(m, m.text == 'x')", LOGS_SCHEMA)
    payload = "__import__('os').system('touch " + str(tmp_path / "pwned") + "')"
    ok, _ = check.is_satisfied_by(
        {"path": "p", "count": 1, "matches": [{"file": "f", "line_no": 1, "text": payload}]},
        {},
        {},
    )
    assert ok is False
    assert not (tmp_path / "pwned").exists()
    # An evaluation error is (False, why), never an exception.
    bad = COMPILER.compile("result.count / 0 > 1", LOGS_SCHEMA)
    ok, why = bad.is_satisfied_by({"path": "p", "count": 1, "matches": []}, {}, {})
    assert ok is False
    assert "could not be evaluated" in why


# -- runbook loading ----------------------------------------------------------


def test_runbook_customer_comms_rejected(tmp_path: Path) -> None:
    body = RUNBOOK % {"stop": "false"} + (
        "comms_if_supported: {audience: [customer], template: pool-customer}\n"
    )
    rb_dir = _write_runbook(tmp_path / "rb", body)
    with pytest.raises(ConfigError, match="internal"):
        load_runbooks(rb_dir, hypotheses=HYPOTHESES, tool_schemas=SCHEMAS, compiler=COMPILER)
    mixed = _write_runbook(
        tmp_path / "rb2",
        RUNBOOK % {"stop": "false"}
        + "comms_if_supported: {audience: [internal, customer], template: t}\n",
    )
    with pytest.raises(ConfigError):
        load_runbooks(mixed, hypotheses=HYPOTHESES, tool_schemas=SCHEMAS, compiler=COMPILER)


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("hypothesis: dependency_failure", "hypothesis: gremlins", "unknown hypothesis"),
        ("tool: airflow.dag_runs", "tool: shell.exec", "not registered"),
        ('"$service.dags[0]"', '"$service.secrets[0]"', "not a catalog field"),
        ("size(result.failed_tasks) > 0", "size(result.failures) > 0", "failures"),
    ],
)
def test_runbook_load_time_checks(tmp_path: Path, old: str, new: str, match: str) -> None:
    body = (RUNBOOK % {"stop": "false"}).replace(old, new)
    rb_dir = _write_runbook(tmp_path / "rb", body)
    with pytest.raises(ConfigError, match=match):
        load_runbooks(rb_dir, hypotheses=HYPOTHESES, tool_schemas=SCHEMAS, compiler=COMPILER)


def test_shipped_runbooks_load() -> None:
    loaded = load_runbooks(
        ROOT / "runbooks", hypotheses=HYPOTHESES, tool_schemas=SCHEMAS, compiler=COMPILER
    )
    assert [rb.id for rb, _ in loaded] == ["runbook:db-pool"]


# -- entailment (§5.4) --------------------------------------------------------


def test_entail_failure_tags_speculation() -> None:
    tools = FakeTools({"gha.recent_runs": FAILED_RUN, "logs.grep": POOL_LOGS})
    # The entail slot fails (None): the claim is speculation, the outcome stands.
    artifact = _service(_knowledge(ROOT / "runbooks"), tools, ScriptedLLM({})).run(
        _case(), Budget()
    )
    assert artifact.outcome == "SUPPORTED"
    assert len(artifact.claims) == 1
    claim = artifact.claims[0]
    assert "speculation" in claim.tags
    assert not claim.sendable
    # "unknown" is also speculation, and only "supports" is sendable.
    unknown = Claim("x", ["s1"])
    apply_verdict(unknown, "unknown")
    assert unknown.tags == {"speculation"}
    ok = Claim("x", ["s1"])
    apply_verdict(ok, "supports")
    assert ok.sendable
    # Over the claim budget, the remaining claims are speculation without a call.
    many = _service(
        _knowledge(ROOT / "runbooks"),
        FakeTools({"gha.recent_runs": FAILED_RUN, "logs.grep": POOL_LOGS}),
        ScriptedLLM({SlotName.ENTAIL: _supports}),
    )
    many._max_claims = 0
    capped = many.run(_case(), Budget())
    assert capped.claims[0].tags == {"speculation"}


def test_contradicted_claim_flagged_not_sent() -> None:
    tools = FakeTools({"gha.recent_runs": FAILED_RUN, "logs.grep": POOL_LOGS})
    llm = ScriptedLLM({SlotName.ENTAIL: lambda _: {"verdict": "contradicts"}})
    artifact = _service(_knowledge(ROOT / "runbooks"), tools, llm).run(_case(), Budget())
    # The verdict acts on the claim, never the outcome.
    assert artifact.outcome == "SUPPORTED"
    claim = artifact.claims[0]
    assert claim.tags == {"speculation", "contradicted"}
    assert not claim.sendable
    shown = artifact.as_dict()["claims"]
    assert shown == [
        {
            "text": claim.text,
            "cites": ["s1"],
            "tags": ["contradicted", "speculation"],
            "sendable": False,
        }
    ]


# -- evidence-source gating and the prior (§5.5) -------------------------------


def test_unregistered_tool_hypothesis_never_supported(tmp_path: Path) -> None:
    # A runbook step whose tool isn't registered never runs, so it can't support.
    rb_dir = _write_runbook(tmp_path / "rb", RUNBOOK % {"stop": "true"})
    tools = FakeTools({"airflow.dag_runs": DAG_OK})  # no gha registered
    artifact = _service(_knowledge(rb_dir), tools, ScriptedLLM({SlotName.ENTAIL: _supports})).run(
        _case(), Budget()
    )
    assert artifact.outcome == "INCONCLUSIVE"
    assert artifact.evidence[0].step.ran is False
    assert "not registered" in artifact.evidence[0].step.why
    assert "gha.recent_runs" not in tools.calls
    # No change feed registered: the change hypothesis has no probe at all.
    knowledge = _knowledge(registered=frozenset({"logs.grep"}))
    assert "recent_change_push" not in knowledge.probes
    llm = ScriptedLLM(
        {SlotName.PROPOSE_HYPOTHESES: lambda _: {"hypotheses": ["recent_change_push"]}}
    )
    proposed = _service(knowledge, FakeTools({"logs.grep": POOL_LOGS}), llm).run(
        _case(classification="network/dns"), Budget()
    )
    assert proposed.outcome == "INCONCLUSIVE"
    assert proposed.reason == "no testable plan"


def test_freeze_zeroes_change_prior() -> None:
    prior = {"recent_change_push": 0.35, "capacity_decay": 0.2}
    ordered, zeroed = order_by_prior(["capacity_decay", "recent_change_push"], prior, freeze=False)
    assert (ordered, zeroed) == (["recent_change_push", "capacity_decay"], False)
    ordered, zeroed = order_by_prior(["recent_change_push", "capacity_decay"], prior, freeze=True)
    assert (ordered, zeroed) == (["capacity_decay", "recent_change_push"], True)
    # The service logs the zeroing as a prior_zeroed event.
    events = RecordingEventSink()
    llm = ScriptedLLM(
        {
            SlotName.PROPOSE_HYPOTHESES: lambda _: {
                "hypotheses": ["recent_change_push", "capacity_decay"]
            }
        }
    )
    tools = FakeTools({"gha.recent_runs": GREEN_RUN, "logs.grep": QUIET_LOGS})
    _service(_knowledge(), tools, llm, events=events, freeze=True).run(
        _case(classification="network/dns"), Budget()
    )
    assert tools.calls == ["logs.grep", "gha.recent_runs"]
    zeroed_events = [e for e in events.appended if e.kind is EventKind.PRIOR_ZEROED]
    assert len(zeroed_events) == 1
    assert zeroed_events[0].payload["hypothesis"] == "recent_change_push"


def test_case_view_shows_contradicted_claims(
    db: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    oidc = FakeOIDC()
    client: TestClient = make_client(db, clock, ids, master_key, oidc, catalog("payments"))
    seed_proposal(db, proposal_id="p_1", case_id="case_1", service="payments", sys_id="INC1")
    tools = FakeTools({"gha.recent_runs": FAILED_RUN, "logs.grep": POOL_LOGS})
    llm = ScriptedLLM({SlotName.ENTAIL: lambda _: {"verdict": "contradicts"}})
    artifact = _service(_knowledge(ROOT / "runbooks"), tools, llm).run(_case(), Budget())
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rca_artifacts (id, tenant_id, case_id, outcome, artifact_json, "
                "created_ts) VALUES ('rca_1', 'acme', 'case_1', :o, :a, 0)"
            ),
            {"o": artifact.outcome, "a": json.dumps(artifact.as_dict())},
        )
    login(client, oidc, ADA)
    page = client.get("/ui/cases/case_1").text
    assert "SUPPORTED" in page
    assert "contradicted by its evidence; never sent" in page
    assert "Internal only" in page
