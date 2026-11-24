"""System D self-onboards end to end (ADR-0022 §5, ADR-0023; WP-211, WP-226).

Canonical test IDs: ``test_system_d_onboarding_lab_runs`` (WP-211) and
``test_system_d_self_onboards_end_to_end`` (WP-226).

System D (``evals/live/system-d``) is an alerts-only Compose streaming stack:
feed -> broker -> writer (with its cursor) -> store, read by an api, and a backfill
run by hand. The installer's answers point an instance at its checkout and its
Prometheus and Loki; from there the instance onboards with nobody's help, a person
reviews with one click per list, and two incidents happen. Nothing here has a model:
the run in CI shows what structure alone gives.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.tools.signal_history import HttpSignalHistory
from supportagent.app.graph_review import GraphReview
from supportagent.bootstrap import (
    _UnrecordedProvider,
    build_worker,
    catalog_review_for,
    detector_review_for,
    onboarding_control_for,
    run_autopilot,
)
from supportagent.config.loader import load_config
from supportagent.config.local_install import parse_env
from supportagent.entrypoints.cli.main import app
from tests.unit.api_support import ADA, TENANT, login, make_client
from tests.unit.system_d import ROOT, SystemD

pytestmark = pytest.mark.unit

_KEY = b"test" * 8
_SOURCE = (ROOT / "sources" / "git").resolve()


def _install(tmp_path: Path) -> tuple[Any, str]:
    """The installer's answers for an alerts-only system: its config and manifest."""
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "team_name": "Readings",
                "llm": {"kind": "anthropic", "model_id": "claude-sonnet-4-5-20250929"},
                "servicenow": {"kind": "none"},
                "onboard": {"system_name": "Meter readings", "local_dir": str(_SOURCE)},
                "signals": {
                    "prometheus_url": "http://prometheus:9090",
                    "loki_url": "http://loki:3100",
                },
                "admin_password": "pw-1",
            }
        )
    )
    out = tmp_path / "local"
    result = CliRunner().invoke(app, ["init", "--dir", str(out), "--answers", str(answers)])
    assert result.exit_code == 0, result.output
    config = load_config(out / "supportagent.yaml", parse_env((out / ".env").read_text()))
    config = config.model_copy(
        update={
            "tenant": config.tenant.model_copy(update={"id": TENANT}),
            "paths": config.paths.model_copy(update={"catalog": str(out / "catalog.yaml")}),
            "onboarding": config.onboarding.model_copy(update={"workspace": str(tmp_path / "ws")}),
        }
    )
    # The checkout is mounted at /mnt/system in the container; here it is where it is.
    manifest = (out / "system.yaml").read_text().replace("file:/mnt/system", f"file:{_SOURCE}")
    return config, manifest


def _history(config: Any, system: SystemD) -> HttpSignalHistory:
    tools = config.tools
    return HttpSignalHistory(
        prometheus=httpx.Client(base_url=tools.prometheus.base_url, transport=system.transport),
        loki=httpx.Client(base_url=tools.loki.base_url, transport=system.transport),
    )


def _onboard(config: Any, history: HttpSignalHistory, db: Any, clock: Any) -> Any:
    status = run_autopilot(
        config,
        engine=db,
        master_key=_KEY,
        clock=clock,
        llm_provider=_UnrecordedProvider(),
        signal_history=history,
    )
    assert status is not None
    return status


def _facts(db: Any, clock: Any) -> tuple[set[str], set[tuple[str, str, str]]]:
    facts = GraphReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT).facts()
    nodes = {f.src for f in facts if f.fact == "node"}
    edges = {(f.src, f.kind, f.dst or "") for f in facts if f.fact == "edge"}
    return nodes, edges


def _name(ref: str) -> str:
    """What a file must contain for a fact about ``ref``: ``job:writer.rollup_hourly``
    -> ``rollup_hourly``, ``topic:readings.raw`` -> ``readings.raw``."""
    _, _, name = ref.partition(":")
    return name.rpartition(".")[2] if ref.startswith("job:") else name


def test_system_d_onboarding_lab_runs() -> None:
    """The fixture is self-consistent: each fact's files name what it claims, every
    incident is caught by a detector the truth expects, and the monitoring answers."""
    truth = SystemD(now=lambda: datetime(2026, 10, 20, 9, tzinfo=UTC)).truth
    graph = truth["graph"]
    for item in graph["nodes"]:
        for source in item["sources"]:
            assert _name(item["id"]) in (_SOURCE / source).read_text(), (item["id"], source)
    nodes = {n["id"] for n in graph["nodes"]}
    for edge in graph["edges"]:
        assert {edge["src"], edge["dst"]} <= nodes, edge
        for source in edge["sources"]:
            text = (_SOURCE / source).read_text()
            assert _name(edge["dst"]) in text, (edge, source)
    proposed = truth["detectors"]["proposed"]
    for scenario, expected in truth["incidents"].items():
        assert set(expected["fires"]) <= set(proposed), scenario
        assert expected["case_service"] in {c["name"] for c in truth["catalog"]}, scenario
    assert set(truth["running"]).isdisjoint(truth["defined_not_running"])
    services = {n["id"].partition(":")[2] for n in graph["nodes"] if n["kind"] == "Service"}
    assert services == {*truth["running"], *truth["defined_not_running"]}


def test_system_d_self_onboards_end_to_end(  # noqa: PLR0915 — one story, start to end
    tmp_path: Path, db: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    config, manifest = _install(tmp_path)
    system = SystemD(now=clock.now)
    truth = system.truth
    history = _history(config, system)

    # The installer hands the system to the instance; the worker's autopilot does the rest.
    control = onboarding_control_for(config, db, clock)
    control.save_manifest(manifest, actor="installer")
    control.request_run(actor="installer")
    status = _onboard(config, history, db, clock)
    steps = {s.step: s for s in status.steps}
    assert steps["ingest"].state == "done", steps["ingest"].detail
    sensed = steps["sense"].detail.removeprefix("plugins: ").split(", ")
    assert sorted(sensed) == truth["plugins"], steps["sense"].detail
    assert steps["graph"].on == "model", "only the summaries wait, on a model CI doesn't have"
    for step in ("catalog", "detectors", "incidents", "readiness", "gaps"):
        assert steps[step].state == "done", (step, steps[step].detail)

    # The graph: exactly what the sources say.
    nodes, edges = _facts(db, clock)
    assert nodes == {n["id"] for n in truth["graph"]["nodes"]}
    assert edges == {(e["src"], e["kind"], e["dst"]) for e in truth["graph"]["edges"]}

    # The catalog: one service each, the scheduled job folded into its owner.
    review = catalog_review_for(config, db, clock)
    drafted = {d["entry"]["name"]: d["entry"]["resources"] for d in review.state()["drafts"]}
    assert drafted == {c["name"]: c["resources"] for c in truth["catalog"]}

    # Detectors: measured from System D's own week, never a default.
    detectors = detector_review_for(config, db, clock)
    state = detectors.state()
    assert state["observed"] == truth["running"]
    measured = {d["id"]: d for d in state["detectors"]}
    expected = truth["detectors"]
    assert set(measured) == set(expected["proposed"])
    for det_id, minutes in expected["proposed"].items():
        assert measured[det_id]["measured"].get("threshold_min") == minutes, det_id
    for det_id, n in expected["suspected_outages"].items():
        assert len(measured[det_id]["measured"]["suspected_outages"]) == n, det_id
    for det_id, service in expected["service"].items():
        assert measured[det_id]["service"] == service, det_id
    assert sorted(n["subject"] for n in state["not_proposed"]) == expected["not_proposed"]
    assert not any("consumer_offsets" in q for q in measured), "Kafka's own topics"

    needs = {n.how for n in status.needs}
    assert {"/ui/onboarding/catalog", "/ui/onboarding/detectors"} <= needs
    assert any(h.startswith("/ui/onboarding/graph") or h == "/ui/onboarding" for h in needs)

    # A person reviews: one click per list.
    oidc = FakeOIDC()
    http = make_client(db, clock, ids, master_key, oidc)
    login(http, oidc, ADA)
    page = http.get("/ui/system").text
    assert "backfill" in page
    token = http.cookies["sa_csrf"]
    for path, form in (
        ("/ui/onboarding/facts", {"every": "confirmed"}),
        ("/ui/onboarding/catalog", {"all": "1", "paging_all": "none:readings-team"}),
        ("/ui/onboarding/detectors", {"all": "on"}),
    ):
        done = http.post(path, data={"csrf_token": token, **form}, follow_redirects=False)
        assert done.status_code == 303, (path, done.text[:300])
    assert {e.name for e in review.accepted()} == set(drafted)
    assert {d.id for d in detectors.enabled("internal")} == set(expected["proposed"])

    clock.advance(3600)
    again = _onboard(config, history, db, clock)
    left = {n.how for n in again.needs}
    assert not left & {"/ui/onboarding/catalog", "/ui/onboarding/detectors"}, left

    # Two incidents. The worker evaluates the detectors and triages what fires.
    worker = build_worker(
        config,
        engine=db,
        master_key=master_key,
        clock=clock,
        ids=ids,
        llm_provider=_UnrecordedProvider(),
        signals=history,
    )
    jobs = {j.name: j for j in worker.jobs}

    def tick() -> None:
        jobs["detectors.evaluate"].run()
        jobs["webhooks.drain"].run()

    def firing() -> set[str]:
        return {d for d, w in _watch_state(db).items() if w.get("fired")}

    tick()
    assert firing() == set(), "a normal hour: nothing fires"
    for name, expected_incident in truth["incidents"].items():
        system.start(name)
        fired_at: dict[str, int] = {}
        for minute in range(1, expected_incident["within_min"] + 1):
            clock.advance(60)
            tick()
            for det in firing():
                fired_at.setdefault(det, minute)
        assert set(fired_at) == set(expected_incident["fires"]), (name, fired_at)
        services = _case_services(db)
        assert expected_incident["case_service"] in services, (name, services)
        system.stop()
        for _ in range(30):
            clock.advance(60)
            tick()
        assert firing() == set(), f"{name}: recovered, every detector resolved"


def _watch_state(db: Any) -> dict[str, Any]:
    with db.connect() as conn:
        raw = conn.execute(
            sa.text("SELECT watch_json FROM detector_watch WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar()
    found = json.loads(raw).get("watches", {}) if raw else {}
    return found if isinstance(found, dict) else {}


def _case_services(db: Any) -> set[str]:
    with db.connect() as conn:
        return {
            str(s)
            for s in conn.execute(
                sa.text("SELECT service FROM cases WHERE tenant_id = :t AND origin = 'alert'"),
                {"t": TENANT},
            ).scalars()
            if s
        }
