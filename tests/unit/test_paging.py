"""WP-112: ``enrich_page`` and ``page_recommended`` (ARCH §14).

Canonical test IDs from 11-wp §WP-112 that live here:
``test_enrich_page_attaches_to_existing_page``, ``test_enrich_page_noop_when_no_page``,
``test_page_recommended_emitted_when_missing_page``,
``test_triage_refused_without_paging_integration``.
``test_create_page_never_executable`` is the INV-01 trigger regression in
``tests/invariants/test_inv01_gate.py``; the handler-level half is here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.paging.noop import NullPaging, RoutedPaging
from supportagent.adapters.paging.opsgenie import OpsgeniePaging
from supportagent.adapters.paging.pagerduty import PagerDutyPaging
from supportagent.app.handlers import (
    HANDLERS,
    NO_OPEN_PAGE,
    EnrichPageHandler,
    build_handlers,
    marker,
)
from supportagent.app.propose import build_action_specs
from supportagent.domain.errors import ExternalServiceError
from supportagent.domain.events import EventKind
from supportagent.domain.kinds import NEVER_EXECUTABLE_KINDS, ActionKind
from supportagent.domain.model.case import Case
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.model.event import Event
from supportagent.domain.model.external import ExternalRef
from supportagent.domain.model.preclass import PreclassRule, RuleSet
from supportagent.domain.model.triage import SafeDefaulted, Triaged
from supportagent.domain.routing import Route
from supportagent.domain.severity import Severity
from supportagent.ports.paging import PageRef, PagingIntegration
from supportagent.ports.tickets import RecordType
from tests.unit.api_support import ADA, TENANT, catalog, login, make_client, seed_proposal
from tests.unit.test_triage_service import _catalog, _evt, _llm_json, _wire

_KEY = "obx-key-1"
_INTEGRATION = "pagerduty:PSVC1"


class _FakePD:
    """A tiny PagerDuty REST API: incidents by service and key, notes per incident."""

    def __init__(self, incidents: list[dict[str, Any]]) -> None:
        self.incidents = incidents
        self.notes: dict[str, list[dict[str, Any]]] = {}
        self.posts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/incidents":
            key = request.url.params.get("incident_key")
            services = request.url.params.get_list("service_ids[]")
            found = [
                i for i in self.incidents if i["incident_key"] == key and i["service"] in services
            ]
            return httpx.Response(200, json={"incidents": found})
        if path.endswith("/notes"):
            incident = path.split("/")[2]
            if request.method == "GET":
                return httpx.Response(200, json={"notes": self.notes.get(incident, [])})
            self.posts += 1
            content = json.loads(request.content)["note"]["content"]
            note = {"id": f"N{self.posts}", "content": content}
            self.notes.setdefault(incident, []).append(note)
            return httpx.Response(201, json={"note": note})
        return httpx.Response(404)

    def client(self) -> PagerDutyPaging:
        transport = httpx.MockTransport(self.handler)
        return PagerDutyPaging(client=httpx.Client(base_url="https://pd.test", transport=transport))


def _payload(source_ref: str = "INC0000001") -> dict[str, object]:
    return {
        "template_id": "enrich_page_v1",
        "integration": _INTEGRATION,
        "source_ref": source_ref,
        "text": "SupportAgent case case_1: payments / P1 / payments/api",
    }


def _execute(paging: PagingIntegration, payload: dict[str, object]) -> ExternalRef:
    return EnrichPageHandler(paging).execute(
        sink=None,  # type: ignore[arg-type]  # a page isn't a ticket: the sink is unused
        table=RecordType.INCIDENT,
        external_id="sys-1",
        payload=payload,
        idempotency_key=_KEY,
    )


def test_enrich_page_attaches_to_existing_page() -> None:
    pd = _FakePD(
        [
            {"id": "PINC1", "incident_key": "INC0000001", "service": "PSVC1"},
            {"id": "POTHER", "incident_key": "INC0000001", "service": "PSVC2"},
        ]
    )
    paging = RoutedPaging([pd.client()])
    ref = _execute(paging, _payload())
    assert ref.as_text() == "pagerduty:PINC1/notes/N1"
    note = pd.notes["PINC1"][0]["content"]
    assert note.splitlines()[-1] == marker(_KEY)  # the INV-04 marker is the last line
    assert "POTHER" not in pd.notes  # another service's page is never touched
    # A re-run (a reclaimed row) finds its own note instead of adding a second.
    again = _execute(paging, _payload())
    assert again.as_text() == "pagerduty:PINC1/notes/N1"
    assert pd.posts == 1


def test_enrich_page_noop_when_no_page() -> None:
    pd = _FakePD([{"id": "PINC1", "incident_key": "other-key", "service": "PSVC1"}])
    assert _execute(RoutedPaging([pd.client()]), _payload()) == NO_OPEN_PAGE
    assert pd.posts == 0
    # No provider for the integration: the Null Object, still a no-op.
    assert _execute(RoutedPaging([]), _payload()) == NO_OPEN_PAGE
    assert NullPaging().supports(_INTEGRATION) is False
    with pytest.raises(ExternalServiceError):
        NullPaging().add_note(PageRef("pagerduty", "P1"), "x")


def test_enrich_page_retryable_on_provider_outage() -> None:
    def down(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    paging = PagerDutyPaging(
        client=httpx.Client(base_url="https://pd.test", transport=httpx.MockTransport(down))
    )
    with pytest.raises(ExternalServiceError) as exc:
        _execute(paging, _payload())
    assert exc.value.retryable


def test_opsgenie_finds_open_alert_by_alias_for_the_team() -> None:
    alerts = {
        "INC0000001": {
            "id": "OG1",
            "status": "open",
            "responders": [{"type": "team", "name": "payments"}],
            "tags": [],
        },
        "closed-1": {"id": "OG2", "status": "closed", "responders": [], "tags": ["payments"]},
        "other-team": {"id": "OG3", "status": "open", "responders": [], "tags": ["search"]},
    }
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        if request.method == "GET" and len(parts) == 4:
            assert request.url.params["identifierType"] == "alias"
            alert = alerts.get(parts[3])
            return httpx.Response(200, json={"data": alert}) if alert else httpx.Response(404)
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        posted.append(json.loads(request.content)["note"])
        return httpx.Response(202, json={"requestId": "r1"})

    og = OpsgeniePaging(
        client=httpx.Client(base_url="https://og.test", transport=httpx.MockTransport(handler))
    )
    assert og.find_open_page("opsgenie:payments", "INC0000001") == PageRef("opsgenie", "OG1")
    assert og.find_open_page("opsgenie:payments", "closed-1") is None
    assert og.find_open_page("opsgenie:payments", "other-team") is None
    assert og.find_open_page("opsgenie:payments", "missing") is None
    payload = {**_payload(), "integration": "opsgenie:payments"}
    assert _execute(RoutedPaging([og]), payload).as_text() == "opsgenie:OG1/notes/r1"
    assert posted[0].endswith(marker(_KEY))


def test_create_page_never_has_a_handler() -> None:
    """ARCH §14: no handler, and no port method, can create a page."""
    handlers = build_handlers(NullPaging())
    assert ActionKind.CREATE_PAGE in NEVER_EXECUTABLE_KINDS
    assert ActionKind.CREATE_PAGE not in handlers
    assert set(handlers) == set(HANDLERS) | {ActionKind.ENRICH_PAGE}
    assert not [m for m in dir(PagingIntegration) if "create" in m or "trigger" in m]


def test_enrich_page_spec_targets_the_alert_dedup_key() -> None:
    case = Case(
        id="case_1",
        tenant_id="acme",
        fingerprint="fp",
        origin="alert",
        service="payments",
        external_table="incident",
        external_id="INC0000001",
    )
    route = Route(id="r", actions=[ActionKind.ENRICH_PAGE])
    (spec,) = build_action_specs(route, case, _catalog())
    assert spec.scope == "service:payments:enrich_page"
    assert spec.payload["integration"] == "pagerduty:payments"
    assert spec.payload["source_ref"] == "INC0000001"


# -- page_recommended (through TriageService) --------------------------------


class _Paging:
    """PagingIntegration stand-in: a page or none, or a lookup failure."""

    def __init__(self, page: PageRef | None = None, *, fail: bool = False) -> None:
        self.page = page
        self.fail = fail
        self.lookups: list[tuple[str, str]] = []

    def supports(self, integration: str) -> bool:
        return integration.startswith("pagerduty:")

    def find_open_page(self, integration: str, source_ref: str) -> PageRef | None:
        self.lookups.append((integration, source_ref))
        if self.fail:
            raise ExternalServiceError("pd down", retryable=True)  # noqa: TRY003
        return self.page

    def find_note(self, page: PageRef, marker: str) -> ExternalRef | None:
        return None

    def add_note(self, page: PageRef, text: str) -> ExternalRef:
        raise AssertionError("triage never writes to a page")  # noqa: TRY003


def _triage(  # noqa: PLR0913, PLR0917 — the fixtures plus the paging seam
    uow: Any, clock: Any, ids: Any, aead: Any, paging: Any, severity: str = "P2"
) -> tuple[Any, Callable[[], Any]]:
    fake = FakeLLMProvider()
    fake.respond(_llm_json(severity=severity))
    w = _wire(uow, clock, ids, aead, fake)
    # A pre-classifier label that agrees, so the P1/P2 is confirmed and routed.
    w.triage._classifier.rule_set = RuleSet(  # type: ignore[attr-defined]
        rules=[PreclassRule(when={"subject": "Payment"}, label="payments/api")]
    )
    w.triage._paging = paging
    # ADR-0009: the case's severity is the source priority, so it matches the LLM's here.
    return w, lambda: w.triage.handle(_evt(severity_hint=Severity(severity)))


def _page_events(db: Any) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM events WHERE kind = 'page_recommended'")
            ).scalar_one()
        )


def _work_notes(db: Any) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT payload_json FROM proposals WHERE action_kind = 'work_note'")
        ).all()
    return [json.loads(r[0]) for r in rows]


def test_page_recommended_emitted_when_missing_page(
    uow: Any, clock: Any, ids: Any, aead: Any, db: Any
) -> None:
    paging = _Paging(page=None)
    _, handle = _triage(uow, clock, ids, aead, paging)
    outcome = handle()
    assert isinstance(outcome, Triaged)
    assert paging.lookups == [("pagerduty:payments", "INC0000001")]
    assert _page_events(db) == 1
    notes = [n for n in _work_notes(db) if n.get("page_recommended")]
    assert len(notes) == 1
    assert "Page recommended" in notes[0]["text"]
    assert "never pages" in notes[0]["text"]
    assert notes[0]["severity"] == "P2"
    # Nothing in the proposals can create a page.
    with db.connect() as conn:
        kinds = {r[0] for r in conn.execute(text("SELECT action_kind FROM proposals")).all()}
    assert ActionKind.CREATE_PAGE.value not in kinds


def test_page_recommended_not_emitted_when_page_exists_or_low_severity(
    uow: Any, clock: Any, ids: Any, aead: Any, db: Any
) -> None:
    _, handle = _triage(uow, clock, ids, aead, _Paging(page=PageRef("pagerduty", "PINC1")))
    assert isinstance(handle(), Triaged)
    assert _page_events(db) == 0
    assert not [n for n in _work_notes(db) if n.get("page_recommended")]


def test_page_recommended_skips_p3(uow: Any, clock: Any, ids: Any, aead: Any, db: Any) -> None:
    paging = _Paging(page=None)
    _, handle = _triage(uow, clock, ids, aead, paging, severity="P3")
    assert isinstance(handle(), Triaged)
    assert paging.lookups == []
    assert _page_events(db) == 0


def test_page_lookup_failure_still_recommends(
    uow: Any, clock: Any, ids: Any, aead: Any, db: Any
) -> None:
    _, handle = _triage(uow, clock, ids, aead, _Paging(fail=True))
    assert isinstance(handle(), Triaged)
    notes = [n for n in _work_notes(db) if n.get("page_recommended")]
    assert "page lookup failed" in notes[0]["text"]


def test_triage_refused_without_paging_integration(
    uow: Any, clock: Any, ids: Any, aead: Any
) -> None:
    """ARCH §14 / ADR-0006: no paging integration, no triage; nothing is looked up."""
    unpaged = Catalog(
        version="1",
        services=[
            ServiceEntry(name="payments", cmdb_ci=["CIPAYMENTS"], resources=["payments-api"])
        ],
    )
    fake = FakeLLMProvider()
    paging = _Paging(page=None)
    w = _wire(uow, clock, ids, aead, fake, catalog=unpaged)
    w.triage._paging = paging
    outcome = w.triage.handle(_evt())
    assert isinstance(outcome, SafeDefaulted)
    assert outcome.decision.reason == "no-paging-integration"
    assert fake.captured == []
    assert paging.lookups == []


def test_case_view_shows_page_recommended_banner(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, aead: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    oidc = FakeOIDC()
    client = make_client(db, clock, ids, master_key, oidc, catalog("payments"))
    seed_proposal(db, proposal_id="p_1", case_id="case_1", service="payments", external_id="INC1")
    login(client, oidc, ADA)
    assert "Page recommended" not in client.get("/ui/cases/case_1").text
    with db.connect() as conn:
        trace = conn.execute(text("SELECT trace_id FROM cases WHERE id = 'case_1'")).scalar_one()
    with uow:
        SqlEventSink(uow=uow, cipher=aead).append(
            Event(
                id="evt_page",
                tenant_id=TENANT,
                trace_id=str(trace),
                span_id="page",
                parent_span_id=None,
                kind=EventKind.PAGE_RECOMMENDED,
                actor="agent",
                payload={"v": 1, "severity": "P1", "integration": "pagerduty:x", "why": "none"},
                ts=clock.now(),
            )
        )
        uow.commit()
    page = client.get("/ui/cases/case_1").text
    assert "Page recommended." in page
    assert "SupportAgent never pages" in page
