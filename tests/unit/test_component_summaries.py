"""WP-222: component summaries (ADR-0023 §3).

Canonical test IDs from 12-wp WP-222: ``test_summary_quotes_its_facts``,
``test_inferred_fact_needs_quote_and_review``, ``test_summary_cached_by_content_hash``,
``test_onboarding_budget_enforced``. The system is the code layer's synthetic
one (``test_code_layer``); the model is scripted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.files.graph_extract import structured_facts
from supportagent.adapters.files.workspace import Workspace
from supportagent.app.component_summaries import (
    ComponentSummaryStore,
    SummaryAnswer,
    summarize_components,
)
from supportagent.app.graph import GraphBuild
from supportagent.app.graph_review import GraphReview
from supportagent.bootstrap import onboard_graph
from supportagent.domain.graph import build
from supportagent.ports.llm import ProviderRequest, ProviderResponse
from tests.unit.api_support import ADA, TENANT, login, make_client
from tests.unit.test_code_layer import _declared, _system
from tests.unit.test_in_instance_onboarding import _config

pytestmark = pytest.mark.unit

_FEED = {
    "summary": [
        {"text": "Feed publishes ticks and bars to Kafka.", "cites": ["F4", "F5"]},
        {"text": "It writes the bar tables.", "cites": ["F7"]},
        {"text": "It is the best service in the company.", "cites": []},
        {"text": "It also trades.", "cites": ["F99"]},
    ],
    "inferred": [],
}
_TRADER = {
    "summary": [{"text": "Trader consumes ticks and reads bars.", "cites": [1, "F2"]}],
    "inferred": [
        # Its quote is a cited line and its target is known: proposed for review.
        {
            "kind": "consumes",
            "dst": "topic:bars",
            "quote": 'KafkaConsumer(bootstrap_servers="broker',
        },
        # A quote the code doesn't contain, a target nobody found, a kind outside the list.
        {"kind": "consumes", "dst": "topic:ticks.*", "quote": "subscribe(['ticks'])"},
        {"kind": "publishes", "dst": "topic:orders", "quote": "self.consumer = KafkaConsumer"},
        {"kind": "runs_on", "dst": "service:broker", "quote": "self.consumer = KafkaConsumer"},
    ],
}


def _answers(component: str) -> dict[str, Any]:
    if component.startswith("service:feed"):
        return _FEED
    if component.startswith("service:trader "):
        return _TRADER
    return {"summary": [{"text": "It runs.", "cites": ["F1"]}], "inferred": []}


class _Model:
    """A scripted ``summarize_component``: answers by component, counting calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, component: str, facts: str, code: str, known: str) -> SummaryAnswer:
        self.calls.append(component)
        return SummaryAnswer.model_validate(_answers(component))


def _built(files: dict[str, str] | None = None) -> GraphBuild:
    facts = structured_facts(files or _system(), {}, {}, _declared())
    return GraphBuild(graph=build(facts), facts=facts)


def test_summary_quotes_its_facts(db: Any, clock: Any, ids: Any, master_key: bytes) -> None:
    built, run = summarize_components(_built(), _system(), _Model(), None)
    feed = run.summaries["service:feed"]
    assert [s["text"] for s in feed["sentences"]] == [
        "Feed publishes ticks and bars to Kafka.",
        "It writes the bar tables.",
    ], "a sentence citing nothing, or a fact that doesn't exist, is dropped"
    assert feed["dropped"] == 2
    assert feed["facts"]["5"] == "service:feed publishes topic:ticks.*", "what F5 says"
    assert run.summaries["service:trader"]["sentences"][0]["cites"] == [1, 2]

    ComponentSummaryStore(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT).save(
        {"v": 1, "components": run.summaries}
    )
    GraphReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT).save(built, {})
    oidc = FakeOIDC()
    http = make_client(db, clock, ids, master_key, oidc)
    login(http, oidc, ADA)
    page = http.get("/ui/system/component?id=service:feed").text
    assert "Feed publishes ticks and bars to Kafka." in page
    assert 'title="service:feed publishes topic:ticks.*">F5</abbr>' in page
    assert "Feed publishes ticks and bars" in http.get("/ui/system").text, "the card's gist"


def test_inferred_fact_needs_quote_and_review(db: Any, clock: Any) -> None:
    built, run = summarize_components(_built(), _system(), _Model(), None)
    trader = run.summaries["service:trader"]
    assert [(i["kind"], i["dst"]) for i in trader["inferred"]] == [("consumes", "topic:bars")]
    assert trader["inferred"][0]["source"] == "git:platform/trader/app.py:7"
    assert trader["dropped"] == 3, "no quote in the code, an unknown target, a kind not allowed"
    assert run.inferred == 1

    GraphReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT).save(built, {})
    facts = GraphReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT).facts()
    edge = next(
        f for f in facts if (f.src, f.kind, f.dst) == ("service:trader", "consumes", "topic:bars")
    )
    assert edge.status == "proposed" and edge.layer == 3, "a person decides"
    assert edge.sources[0]["quote"] == 'KafkaConsumer(bootstrap_servers="broker'


def test_summary_cached_by_content_hash() -> None:
    model = _Model()
    _, first = summarize_components(_built(), _system(), model, None)
    calls = len(model.calls)
    assert calls == first.written and first.reused == 0
    stored = {"components": first.summaries}

    _, again = summarize_components(_built(), _system(), model, stored)
    assert len(model.calls) == calls, "nothing changed: no model call"
    assert again.reused == calls and again.summaries == first.summaries
    assert again.inferred == 1, "a reused summary keeps its inferred facts"
    _, other = summarize_components(_built(), _system(), model, stored, version="v2:abc")
    assert other.written == calls, "another model or prompt writes every summary again"
    model.calls = model.calls[:calls]

    files = _system()
    files["platform/trader/app.py"] = files["platform/trader/app.py"].replace(
        'ORDER BY ts")', 'ORDER BY ts DESC")'
    )
    _, changed = summarize_components(_built(files), files, model, stored)
    rewritten = [c.split(" ")[0] for c in model.calls[calls:]]
    assert rewritten == ["service:trader", "service:trader-worker"], "built from the changed code"
    assert changed.written == 2 and changed.reused == calls - 2


class _Provider:
    """The model behind the slot: scripted answers, 1000 tokens a call."""

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        component = request.user.split("Component: ", 1)[1].split("\n", 1)[0]
        return ProviderResponse(
            raw_text=json.dumps(_answers(component)),
            resolved_model=request.model_id,
            tokens_in=700,
            tokens_out=300,
            latency_ms=1,
        )


def _workspace(tmp_path: Path, budget: int) -> Path:
    workspace = tmp_path / "ws"
    Workspace(workspace).prepare(
        "system: {id: shop, name: The shop}\n"
        "sources:\n  git:\n    - repo: file:/srv/shop\n      paths: ['**/*.py']\n"
        f"budget: {{llm_tokens: {budget}}}\n"
    )
    for name, text in _system().items():
        path = workspace / "sources" / "git" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return workspace


def test_onboarding_budget_enforced(tmp_path: Path, db: Any, clock: Any) -> None:
    config = _config(tmp_path)
    workspace = _workspace(tmp_path, budget=2000)
    model = _Provider()
    report = onboard_graph(config, workspace, provider=model, engine=db, clock=clock)
    assert model.calls == 2, "the budget is spent after two calls; the rest aren't made"
    assert report.budget_spent and report.summaries is not None
    assert report.summaries.written == 2 and report.summaries.unwritten
    from supportagent.bootstrap import _graph_detail  # noqa: PLC0415

    assert "budget.llm_tokens" in _graph_detail(report)

    (workspace / "system.yaml").write_text(
        (workspace / "system.yaml").read_text().replace("2000", "100000")
    )
    again = onboard_graph(config, workspace, provider=model, engine=db, clock=clock)
    assert again.summaries is not None and not again.summaries.unwritten
    assert again.summaries.reused == 2, "the next run continues where the budget stopped"
    assert not again.budget_spent


def test_an_upgraded_step_runs_again_on_unchanged_sources(
    tmp_path: Path, db: Any, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that changes a step's work (summaries, here) re-runs it after an upgrade."""
    import supportagent.bootstrap as boot  # noqa: PLC0415

    config = _config(tmp_path)
    workspace = _workspace(tmp_path, budget=100000)

    def run() -> dict[str, str]:
        status = boot.onboard_run(
            config,
            workspace / "system.yaml",
            workspace,
            provider=_Provider(),
            engine=db,
            clock=clock,
            master_key=b"test" * 8,
        )
        return {s.step: s.at for s in status.steps}

    first = run()
    clock.advance(60)
    assert run()["graph"] == first["graph"], "unchanged: skipped"
    monkeypatch.setitem(boot._STEP_VERSIONS, "graph", "next")
    clock.advance(60)
    upgraded = run()
    assert upgraded["graph"] != first["graph"], "its work changed: it runs again"
    assert upgraded["sense"] == first["sense"]
