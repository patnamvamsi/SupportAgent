"""WP-101: lifecycle, takeover, hand-back (ARCH §8.1, §8.2; 01 §7.11).

Canonical test IDs from 11-wp §WP-101 that live here:
``test_takeover_supersedes_pending_proposals``, ``test_human_owned_blocks_new_submits``,
``test_hand_back_api_transitions_to_in_progress``,
``test_assisted_mode_requires_confirmed_automation_actors``. The sync-driven
ones run through the worker in ``tests/integration/test_wp101_lifecycle.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.servicenow.semantics import ServiceNowSemantics
from supportagent.app.gate import GateService
from supportagent.app.lifecycle import LifecycleService
from supportagent.app.setup import SetupService
from supportagent.bootstrap import require_automation_decided
from supportagent.domain.errors import ConfigError
from supportagent.domain.kinds import ActionKind
from supportagent.domain.lifecycle import CaseEvent, CaseStatus
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.proposal import Skipped
from supportagent.domain.model.ticket import ChangeRecord
from supportagent.domain.takeover import is_human_actor, lifecycle_signals
from supportagent.domain.trace import TraceContext
from tests.unit.api_support import (
    ADA,
    BOB,
    CARL,
    TENANT,
    catalog,
    csrf,
    login,
    logout,
    make_client,
    seed_proposal,
    seed_users,
)
from tests.unit.test_triage_service import _shadow_config

_T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
_SVC = "svc_supportagent"


def _audit(user: str, field: str, new: str, minute: int = 0, checkpoint: int = 2) -> ChangeRecord:
    return ChangeRecord(
        source="audit",
        user=user,
        ts=_T0 + timedelta(minutes=minute),
        fieldname=field,
        newvalue=new,
        checkpoint=checkpoint,
    )


def _signals(changes: list[ChangeRecord], status: CaseStatus, **kw: Any) -> list[CaseEvent]:
    return [
        s.event
        for s in lifecycle_signals(
            changes,
            status,
            integration_user=_SVC,
            integration_ids=kw.get("ids", {_SVC, "u-svc"}),
            automation_actors=kw.get("automation", set()),
            semantics=ServiceNowSemantics.with_resolved_states(kw.get("resolved")),
        )
    ]


def test_custom_resolved_states() -> None:
    """BL-032: an instance whose resolved state is "100" resolves on it, not on "6"."""
    resolved = _audit("system", "state", "100")
    assert _signals([resolved], CaseStatus.TRIAGED) == []  # not a resolved state by default
    assert _signals([resolved], CaseStatus.TRIAGED, resolved={"100"}) == [CaseEvent.RESOLVED]
    default_six = _audit("system", "state", "6")
    assert _signals([default_six], CaseStatus.TRIAGED, resolved={"100"}) == []
    assert _signals([default_six], CaseStatus.TRIAGED) == [CaseEvent.RESOLVED]


# --- domain ------------------------------------------------------------------


def test_human_actor_definition() -> None:
    def human(actor: str) -> bool:
        return is_human_actor(actor, integration_user=_SVC, automation_actors={"flow.designer"})

    assert human("bob")
    assert not human(_SVC)
    assert not human("system")
    assert not human("flow.designer")
    assert not human("")


def test_signals_follow_the_narrow_triggers() -> None:
    triaged = CaseStatus.TRIAGED
    assert _signals([_audit("bob", "assigned_to", "u-bob")], triaged) == [CaseEvent.HUMAN_TAKEOVER]
    assert _signals([_audit("bob", "state", "2")], triaged) == [CaseEvent.HUMAN_TAKEOVER]
    assert _signals([_audit("bob", "priority", "1")], triaged) == [], "other fields aren't takeover"
    comment = ChangeRecord(source="journal", user="bob", ts=_T0, fieldname="comments")
    assert _signals([comment], triaged) == [], "a comment is not takeover"
    assert (
        _signals(
            [_audit("flow.designer", "assigned_to", "u-x")], triaged, automation={"flow.designer"}
        )
        == []
    )
    # An undecided candidate counts as a person (the safe direction).
    assert _signals([_audit("flow.designer", "assigned_to", "u-x")], triaged) == [
        CaseEvent.HUMAN_TAKEOVER
    ]


def test_signals_resolve_reopen_and_hand_back_in_order() -> None:
    history = [
        _audit("bob", "assigned_to", "u-bob", 1),
        _audit("bob", "assigned_to", "u-svc", 2),  # re-assigned to the agent: hand back
        _audit("bob", "state", "6", 3),
        _audit("carol", "state", "2", 4),
    ]
    assert _signals(history, CaseStatus.TRIAGED) == [
        CaseEvent.HUMAN_TAKEOVER,
        CaseEvent.HAND_BACK,
        CaseEvent.RESOLVED,
        CaseEvent.REOPENED,
    ]
    # Replaying from the end state adds nothing illegal.
    assert _signals([_audit("bob", "assigned_to", "u-bob")], CaseStatus.HUMAN_OWNED) == []


# --- service -----------------------------------------------------------------


@pytest.fixture
def lifecycle(db: Any, clock: Any, ids: Any, events: Any) -> LifecycleService:
    return LifecycleService(
        uow=SqlUnitOfWork(db),
        clock=clock,
        ids=ids,
        events=events,
        tenant_id=TENANT,
        integration_user=_SVC,
        integration_ids=lambda: {"u-svc"},
        semantics=ServiceNowSemantics(),
    )


def _status(db: Any, table: str, row_id: str) -> str:
    with db.connect() as conn:
        return str(
            conn.execute(
                text(f"SELECT status FROM {table} WHERE id = :i"),  # noqa: S608
                {"i": row_id},
            ).scalar_one()
        )


def test_takeover_supersedes_pending_proposals(
    db: Any, lifecycle: LifecycleService, events: Any
) -> None:
    seed_proposal(db, proposal_id="prop_1", case_id="case_1")
    seed_proposal(db, proposal_id="prop_2", case_id="case_1", status="executed")
    applied = lifecycle.apply("case_1", [_audit("bob", "assigned_to", "u-bob", checkpoint=4)])
    assert [s.event for s in applied] == [CaseEvent.HUMAN_TAKEOVER]
    assert _status(db, "cases", "case_1") == "human_owned"
    assert _status(db, "proposals", "prop_1") == "superseded"
    assert _status(db, "proposals", "prop_2") == "executed", "only pending ones"
    takeover = [e for e in events.appended if e.kind.value == "sync_takeover"]
    assert len(takeover) == 1
    assert (
        takeover[0].actor,
        takeover[0].payload["field"],
        takeover[0].payload["sys_mod_count"],
    ) == (
        "bob",
        "assigned_to",
        4,
    )
    # Applying the same history again changes nothing.
    assert lifecycle.apply("case_1", [_audit("bob", "assigned_to", "u-bob")]) == []


def test_human_owned_blocks_new_submits(db: Any, clock: Any, ids: Any, events: Any) -> None:
    seed_proposal(db, proposal_id="prop_0", case_id="case_1", status="executed")
    with db.begin() as conn:
        conn.execute(text("UPDATE cases SET status = 'human_owned' WHERE id = 'case_1'"))
    gate = GateService(
        uow=SqlUnitOfWork(db),
        clock=clock,
        ids=ids,
        events=events,
        catalog=catalog("payments"),
        ttl=timedelta(hours=1),
    )
    spec = ActionSpec(
        kind=ActionKind.WORK_NOTE,
        target=TargetRef(service="payments", system="servicenow", ref="INC0000001"),
        scope="service:payments:work_note",
        payload={"template_id": "work_note_v1", "text": "x"},
    )
    result = gate.submit("case_1", spec, TraceContext.new(TENANT, "tr", "sp"))
    assert isinstance(result, Skipped) and result.reason == "human_owned"
    with db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM proposals")).scalar_one() == 1


def test_reopen_refused_when_a_newer_case_holds_the_fingerprint(
    db: Any, lifecycle: LifecycleService
) -> None:
    seed_proposal(db, proposal_id="prop_1", case_id="case_old")
    lifecycle.apply("case_old", [_audit("bob", "state", "6")])
    assert _status(db, "cases", "case_old") == "resolved"
    with db.begin() as conn:  # the same signature fires again: a new case
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, trace_id, "
                "duplicate_count, opened_ts, last_alert_ts) VALUES ('case_new', :t, "
                "'fp_case_old', 'alert', 'open', 'tr_new', 0, 1, 1)"
            ),
            {"t": TENANT},
        )
    assert lifecycle.apply("case_old", [_audit("bob", "state", "2", 5)]) == []
    assert _status(db, "cases", "case_old") == "resolved", "INV-02 keeps one unresolved case"


def test_assisted_mode_requires_confirmed_automation_actors() -> None:
    class _Store:
        def __init__(self, total: int, decided: int) -> None:
            self.counts = (total, decided)

        def add_candidates(self, tenant_id: str, actors: Any) -> int:
            return 0

        def summary(self, tenant_id: str) -> tuple[int, int]:
            return self.counts

    assisted = _shadow_config().model_copy(update={"mode": "assisted"})
    with pytest.raises(ConfigError, match="1 undecided"):
        require_automation_decided(assisted, _Store(3, 2))
    require_automation_decided(assisted, _Store(3, 3))
    require_automation_decided(_shadow_config(), _Store(3, 0))  # shadow runs undecided


# --- API / UI ----------------------------------------------------------------


@pytest.fixture
def oidc() -> FakeOIDC:
    return FakeOIDC()


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes, oidc: FakeOIDC) -> TestClient:
    return make_client(db, clock, ids, master_key, oidc, catalog("payments", "search"))


def _human_owned(db: Any, case_id: str = "case_1", service: str = "payments") -> None:
    seed_proposal(
        db,
        proposal_id=f"p_{case_id}",
        case_id=case_id,
        service=service,
        external_id=f"INC_{case_id}",
    )
    with db.begin() as conn:
        conn.execute(text("UPDATE cases SET status = 'human_owned' WHERE id = :c"), {"c": case_id})


def test_hand_back_api_transitions_to_in_progress(
    client: TestClient, oidc: FakeOIDC, db: Any
) -> None:
    _human_owned(db)
    _human_owned(db, "case_2", "search")
    login(client, oidc, ADA)
    logout(client)
    seed_users(db, ("sub-bob", "analyst", "service:payments"), ("sub-carl", "viewer"))
    login(client, oidc, CARL)
    assert client.post("/api/v1/cases/case_1/hand-back", headers=csrf(client)).status_code == 403
    logout(client)
    login(client, oidc, BOB)
    page = client.get("/ui/cases/case_1").text
    assert "Hand back to the agent" in page
    assert client.post("/api/v1/cases/case_2/hand-back", headers=csrf(client)).status_code == 403
    done = client.post("/api/v1/cases/case_1/hand-back", headers=csrf(client))
    assert done.status_code == 200, done.text
    assert _status(db, "cases", "case_1") == "in_progress"
    again = client.post("/api/v1/cases/case_1/hand-back", headers=csrf(client))
    assert again.status_code == 422, "only a human-owned case can be handed back"


def test_ui_hand_back_form(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    _human_owned(db)
    login(client, oidc, ADA)
    resp = client.post(
        "/ui/cases/case_1/hand-back",
        data={"csrf_token": client.cookies.get("sa_csrf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _status(db, "cases", "case_1") == "in_progress"


def test_setup_panel_decides_automation_actors(  # noqa: PLR0913, PLR0917
    client: TestClient, oidc: FakeOIDC, db: Any, clock: Any, ids: Any, events: Any
) -> None:
    login(client, oidc, ADA)
    with db.begin() as conn:
        for user in ("flow.designer", "alice.agent"):
            conn.execute(
                text(
                    "INSERT INTO automation_actors (tenant_id, sys_user, kind) "
                    "VALUES (:t, :u, 'integration')"
                ),
                {"t": TENANT, "u": user},
            )
    page = client.get("/ui/setup").text
    assert "flow.designer" in page and "undecided" in page
    token = client.cookies.get("sa_csrf")
    for user, decision in (("flow.designer", "automation"), ("alice.agent", "human")):
        resp = client.post(
            f"/ui/setup/automation/{user}",
            data={"csrf_token": token, "decision": decision},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    setup = SetupService(
        uow=SqlUnitOfWork(db),
        clock=clock,
        ids=ids,
        events=events,
        catalog=catalog("payments"),
        tenant_id=TENANT,
    )
    decided = {a["sys_user"]: (a["kind"], a["confirmed_by"]) for a in setup.automation_actors()}
    assert decided == {
        "flow.designer": ("integration", "sub-ada"),
        "alice.agent": ("human", "sub-ada"),
    }
