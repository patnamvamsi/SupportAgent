"""ADR-0018 (WP-E1): live evidence capture, as of each past incident.

The done-when: a workspace whose tools are fakes **with history** captures
evidence that reaches the same readiness as its fixture; a point-in-time tool
and an aged-out store are each reported as such, never guessed.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from supportagent.adapters.files.eval_loader import load_yaml_document
from supportagent.adapters.tools.changes import RecentChangesTool
from supportagent.adapters.tools.gha import GitHubActionsTool
from supportagent.adapters.tools.loki import LokiSearchTool
from supportagent.adapters.tools.prometheus import PrometheusQueryTool
from supportagent.app.tools import ToolRegistry
from supportagent.bootstrap import (
    KNOWN_TOOL_SCHEMAS,
    ONBOARDING_DIR,
    LabPipeline,
    OnboardingRun,
    _readiness_needs,
    build_readiness,
    build_system_graph,
    capture_evidence,
    load_evidence,
    onboard_evidence,
)
from supportagent.config.loader import load_config_offline
from supportagent.domain.errors import HistoryUnavailable, ToolError
from supportagent.domain.evidence import PlannedRead, capture, capture_plan
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.model.runbook import Probe, Runbook
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.domain.trace import TraceContext
from supportagent.ports.tools import ToolResult
from tests.unit.test_readiness import _Classifier, _truth_map

pytestmark = pytest.mark.unit

_LAB = Path(__file__).resolve().parents[2] / "evals/lab/lab.yaml"
_AT = datetime(2025, 11, 8, 22, 52, tzinfo=UTC)
_NOW = datetime(2025, 11, 20, tzinfo=UTC)


# --- what to read, and sorting what came back ---------------------------------------------


def _runbook(label: str, *steps: tuple[str, str, str]) -> Runbook:
    return Runbook.model_validate(
        {
            "id": f"runbook:{label.replace('/', '-')}",
            "applies_when": {"classification": label},
            "steps": [
                {"id": f"s{i}", "hypothesis": h, "tool": t, "args": {"n": a}, "check": "true"}
                for i, (h, t, a) in enumerate(steps)
            ],
        }
    )


def test_the_plan_is_the_label_runbooks_then_the_probe_one_read_per_capability() -> None:
    runbooks = [
        _runbook("db/locks", ("db_lock", "oracle.locks", "1"), ("db_lock", "logs.search", "a")),
        _runbook("db", ("other", "logs.search", "b"), ("db_lock", "metrics.query", "c")),
        _runbook("app/heap", ("heap", "k8s.pod_status", "x")),
    ]
    probe = Probe(tool="changes.recent", args={}, check="true")
    plan = capture_plan("db/locks", "db_lock", runbooks, probe)
    assert [(p.capability, p.cause) for p in plan] == [
        ("oracle.locks", True),
        ("logs.search", True),
        ("metrics.query", True),
        ("changes.recent", True),
    ]
    assert plan[1].args == {"n": "a"}  # the first step's arguments
    assert capture_plan(None, None, runbooks, None) == []


def _capture(plan: list[PlannedRead], read: Any, **kw: Any) -> Any:
    options: dict[str, Any] = {
        "now": _NOW,
        "retention_days": {"logs.search": 30},
        "available": lambda c: c != "airflow.dag_runs",
        "replayable": lambda c: c not in {"oracle.locks"},
        "read": read,
    } | kw
    return capture("INC1", _AT, plan, **options)


def test_capture_sorts_each_answer_honestly() -> None:
    def read(item: PlannedRead) -> Mapping[str, object]:
        if item.capability == "metrics.query":
            raise HistoryUnavailable("gone")
        if item.capability == "gha.recent_runs":
            raise ToolError("403: not allowed")  # noqa: TRY003
        return {"from": item.capability}

    plan = [
        PlannedRead("logs.search", {}, cause=True),
        PlannedRead("oracle.locks", {}, cause=False),
        PlannedRead("metrics.query", {}, cause=False),
        PlannedRead("gha.recent_runs", {}, cause=False),
        PlannedRead("airflow.dag_runs", {}, cause=False),
    ]
    got = _capture(plan, read)
    assert got.world == {
        "logs.search": {"result": {"from": "logs.search"}},
        "gha.recent_runs": {"error": "403: not allowed"},
    }
    assert got.not_replayable == ["oracle.locks"]
    assert got.aged_out == ["metrics.query"]
    assert got.unavailable == ["airflow.dag_runs"]
    assert got.unreplayed is None
    doc = got.as_document()
    assert doc["captured"] == "live" and doc["captured_at"] == "2025-11-08 22:52:00"


def test_without_a_cause_read_the_world_is_left_empty_and_says_why() -> None:
    def read(item: PlannedRead) -> Mapping[str, object]:
        return {"ok": True}

    live = _capture(
        [PlannedRead("oracle.locks", {}, True), PlannedRead("logs.search", {}, True)], read
    )  # one cause read can't see the past: the other alone would mislead the replay
    assert live.world == {} and live.unreplayed == "live_only"
    old = _capture(
        [PlannedRead("logs.search", {}, True)], read, now=_AT + timedelta(days=31)
    )  # older than logs.search keeps
    assert old.world == {} and old.unreplayed == "aged_out" and old.aged_out == ["logs.search"]
    # No cause read planned: nothing to withhold, the world is kept for readiness to judge.
    kept = _capture([PlannedRead("logs.search", {}, False)], read)
    assert kept.world and kept.unreplayed is None


# --- each tool's read, as of a time ----------------------------------------------------------


def _client(seen: list[httpx.Request], body: Any) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body(request) if callable(body) else body)

    return httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))


def test_loki_and_prometheus_read_as_of_the_incident(clock: Any) -> None:
    seen: list[httpx.Request] = []
    loki = LokiSearchTool(
        client=_client(
            seen, {"status": "success", "data": {"resultType": "streams", "result": []}}
        ),
        clock=clock,
    )
    loki.run_as_of({"query": '{service="pay"}', "window_min": 60}, at=_AT, timeout_s=1)
    end = int(seen[-1].url.params["end"])
    assert end == int(_AT.timestamp() * 1e9)
    assert end - int(seen[-1].url.params["start"]) == 60 * 60 * 10**9
    prom = PrometheusQueryTool(client=_client(seen, {"status": "success", "data": {"result": []}}))
    prom.run_as_of({"query": "up"}, at=_AT, timeout_s=1)
    assert float(seen[-1].url.params["time"]) == _AT.timestamp()
    prom.run({"query": "up"}, timeout_s=1)
    assert "time" not in seen[-1].url.params  # live reads are unchanged


def test_gha_lists_runs_created_before_the_incident() -> None:
    seen: list[httpx.Request] = []
    gha = GitHubActionsTool(client=_client(seen, {"workflow_runs": []}))
    gha.run_as_of({"repo": "acme/pay"}, at=_AT, timeout_s=1)
    assert seen[-1].url.params["created"] == "<2025-11-08T22:52:00Z"


def _deploy(at: datetime) -> dict[str, object]:
    return {"sha": "abc", "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "creator": {}}


def test_changes_as_of_drop_later_changes_and_say_when_history_runs_out(clock: Any) -> None:
    deploys = [_deploy(_AT - timedelta(minutes=30)), _deploy(_AT + timedelta(minutes=30))]
    tool = RecentChangesTool(clock=clock, github=_client([], deploys))
    got = tool.run_as_of({"repo": "acme/pay", "window_min": 120}, at=_AT, timeout_s=1)
    assert got.data["count"] == 1
    assert got.data["changes"][0]["minutes_ago"] == 30  # type: ignore[index]
    newer = [_deploy(_AT + timedelta(days=d)) for d in range(1, 31)]  # a full page, all later
    tool = RecentChangesTool(clock=clock, github=_client([], newer))
    with pytest.raises(HistoryUnavailable, match="don't reach back"):
        tool.run_as_of({"repo": "acme/pay", "window_min": 120}, at=_AT, timeout_s=1)


def test_the_registry_reads_the_past_only_through_tools_that_can(
    events: Any, ids: Any, clock: Any
) -> None:
    class _Point:
        name, trust_class, tier = "oracle.locks", TrustClass.READ, Tier.T0
        result_schema: Mapping[str, object] = {"type": "object"}
        literal_args: frozenset[str] = frozenset()

        def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
            return ToolResult({})

    class _Past(_Point):
        name = "logs.search"

        def run_as_of(
            self, args: Mapping[str, object], *, at: datetime, timeout_s: float
        ) -> ToolResult:
            return ToolResult({"at": at.isoformat()})

    registry = ToolRegistry(
        [_Point(), _Past()], events=events, ids=ids, clock=clock, redact=lambda t: t
    )
    service = ServiceEntry(name="pay")
    trace = TraceContext(tenant_id="acme", trace_id="evidence", span_id="capture")
    assert registry.replayable("logs.search") and not registry.replayable("oracle.locks")
    got = registry.call_as_of("logs.search", {}, service, trace, at=_AT)
    assert got.data == {"at": _AT.isoformat()}
    assert [e.payload["tool"] for e in events.appended] == ["logs.search"]  # audited
    with pytest.raises(ToolError, match="can't read the past"):
        registry.call_as_of("oracle.locks", {}, service, trace, at=_AT)


# --- the done-when: System A from fakes with history -----------------------------------------


class _History:
    """A store with history: answers as of an incident's open time with what was true then
    (System A's fixture evidence). Nothing recorded for that time: aged out."""

    trust_class, tier = TrustClass.READ, Tier.T0
    literal_args: frozenset[str] = frozenset({"query", "window_min", "limit", "pattern"})

    def __init__(self, name: str, by_time: Mapping[datetime, Mapping[str, Any]]) -> None:
        self.name = name
        self.result_schema = KNOWN_TOOL_SCHEMAS[name]
        self._by_time = by_time

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        raise AssertionError("capture reads as of a time")  # noqa: TRY003

    def run_as_of(
        self, args: Mapping[str, object], *, at: datetime, timeout_s: float
    ) -> ToolResult:
        entry = self._by_time.get(at, {}).get(self.name)
        if entry is None:
            raise HistoryUnavailable(f"{self.name}: nothing kept for {at}")  # noqa: TRY003
        if entry.get("error"):
            raise ToolError(str(entry["error"]))
        return ToolResult(entry["result"])


class _Now(_History):
    """The same tool, point-in-time: it can only read the present."""

    run_as_of = None  # type: ignore[assignment]

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        return ToolResult({})


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "system-a"
    shutil.copytree(ONBOARDING_DIR / "system-a", workspace)
    shutil.rmtree(workspace / "sources" / "evidence")
    return workspace


def _fixture_by_time() -> dict[datetime, dict[str, Any]]:
    directory = ONBOARDING_DIR / "system-a" / "sources" / "evidence"
    out: dict[datetime, dict[str, Any]] = {}
    for file in sorted(directory.glob("*.yaml")):
        doc = load_yaml_document(file)
        at = datetime.strptime(str(doc["captured_at"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        out[at] = dict(doc["world"])
    return out


def _run(workspace: Path) -> OnboardingRun:
    return OnboardingRun(
        None,  # type: ignore[arg-type] # the result and plan aren't read
        [],
        build_system_graph(workspace, None),
        _truth_map("system-a"),
        None,  # type: ignore[arg-type]
    )


def _registry(events: Any, ids: Any, clock: Any, *, point: frozenset[str] = frozenset()) -> Any:
    by_time = _fixture_by_time()
    names = sorted({n for world in by_time.values() for n in world})
    tools = [(_Now if n in point else _History)(n, by_time) for n in names]
    return ToolRegistry(tools, events=events, ids=ids, clock=clock, redact=lambda t: t)


def _config() -> Any:
    cfg = load_config_offline(_LAB)
    # The fixtures are a year old: these stores are taken to keep everything.
    return cfg.model_copy(
        update={"onboarding": cfg.onboarding.model_copy(update={"retention_days": {}})}
    )


def test_fakes_with_history_reach_the_fixture_readiness(
    tmp_path: Path, events: Any, ids: Any, clock: Any
) -> None:
    cfg = _config()
    fixture = build_readiness(
        ONBOARDING_DIR / "system-a",
        _run(ONBOARDING_DIR / "system-a"),
        LabPipeline(cfg, provider=_Classifier("system-a")),
        cfg,
    )
    workspace = _workspace(tmp_path)
    run = _run(workspace)
    got = capture_evidence(cfg, workspace, run, _registry(events, ids, clock), clock=clock)
    assert got.captured == len(load_evidence(ONBOARDING_DIR / "system-a")), got
    assert all(e.kind.value == "tool_call" for e in events.appended)  # every read audited
    captured = build_readiness(
        workspace, run, LabPipeline(cfg, provider=_Classifier("system-a")), cfg
    )
    assert captured.statuses() == fixture.statuses(), captured.lines()
    assert captured.ready_share == fixture.ready_share
    # Re-running keeps what's there; --recapture reads again.
    again = capture_evidence(cfg, workspace, run, _registry(events, ids, clock), clock=clock)
    assert again.captured == 0 and again.kept == got.captured + sum(got.unreplayed.values())


def test_point_in_time_tools_make_a_class_visible_only_live(
    tmp_path: Path, events: Any, ids: Any, clock: Any
) -> None:
    cfg = _config()
    workspace = _workspace(tmp_path)
    run = _run(workspace)
    oracle = frozenset(n for n in KNOWN_TOOL_SCHEMAS if n.startswith("oracle."))
    got = capture_evidence(
        cfg, workspace, run, _registry(events, ids, clock, point=oracle), clock=clock
    )
    assert got.unreplayed.get("live_only"), got
    report = build_readiness(
        workspace, run, LabPipeline(cfg, provider=_Classifier("system-a")), cfg
    )
    locks = next(c for c in report.classes if c.class_id == "oracle-lock-contention")
    assert locks.status == "untestable"
    assert "visible only live" in locks.reasons[0]
    assert all(why == "live_only" for _, why in locks.unreplayed)
    needs = _readiness_needs(report.as_dict())
    assert any(n.kind == "evidence" and "shadow" in n.how for n in needs)
    assert not any(n.kind == "plugin" and "oracle-lock" in n.what for n in needs)


def test_a_store_past_its_retention_is_aged_out_not_read(
    tmp_path: Path, events: Any, ids: Any, clock: Any
) -> None:
    cfg = load_config_offline(_LAB)  # default retention: logs 30 days, metrics 15
    workspace = _workspace(tmp_path)
    capture_evidence(cfg, workspace, _run(workspace), _registry(events, ids, clock), clock=clock)
    doc = load_yaml_document(workspace / "sources" / "evidence" / "INCA0001.yaml")
    assert "logs.search" in doc["aged_out"]
    assert "logs.search" not in doc["world"]


def test_without_a_tool_that_reads_the_past_nothing_is_written(
    tmp_path: Path, db: Any, clock: Any, master_key: bytes
) -> None:
    workspace = _workspace(tmp_path)
    catalog = tmp_path / "services.yaml"
    catalog.write_text(
        'version: "1"\nservices:\n  - {name: pay, paging_integration: "pagerduty:pay"}\n',
        encoding="utf-8",
    )
    cfg = load_config_offline(_LAB)
    cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"catalog": str(catalog)})})
    got = onboard_evidence(cfg, workspace, engine=db, clock=clock, master_key=master_key)
    assert got is None
    assert not (workspace / "sources" / "evidence").exists()
