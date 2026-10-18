"""INV-01: the gate enforces authorization at the database level.

Frozen test IDs (10-wp WP-005, 04 §5):

trigger / GWT:
- ``test_gate_rejected_approval_blocked``
- ``test_gate_expired_approval_blocked``
- ``test_gate_payload_hash_mismatch_blocked``
- ``test_gate_grant_expired_or_out_of_scope_blocked``
- ``test_gate_grant_pending_not_enforceable``
- ``test_gate_approval_single_use``
- ``test_outbox_immutable_after_insert``
- ``test_customer_comm_never_by_policy_grant``
- ``test_comms_plan_revoked_blocked``
- ``test_create_page_never_executable``
- ``test_self_approval_blocked``
- ``test_trigger_kind_lists_match_code``

behaviour (GateService):
- ``test_gate_submit_drops_unknown_service_target``
- ``test_gate_submit_grant_path_auto_approves_internal_kind``
- ``test_gate_submit_customer_kind_never_grant_path``
- ``test_gate_approve_stale_hash_conflict``
- ``test_gate_approve_concurrent_second_fails``
- ``test_gate_expire_due_marks_expired``

The trigger tests assert engine-agnostic behaviour: the DB raises a DBAPI error
whose message starts with ``INV-01:`` (SQLite ``RAISE(ABORT, ...)`` surfaces as
``IntegrityError``; Postgres ``RAISE EXCEPTION ... ERRCODE='P0001'`` surfaces
as ``ProgrammingError``). Both are subclasses of ``sa.exc.DBAPIError``.

Grant time-validity is checked by the trigger against the *database* wall clock,
so seed grants with unambiguous epochs: ``PAST_TS`` (long expired) and
``FUTURE_TS`` (definitely valid).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from supportagent.adapters.db.repos.outbox import OutboxRepo
from supportagent.app.gate import GateService
from supportagent.domain.errors import (
    AuthorizationDenied,
    ConcurrencyConflict,
    DomainValidationError,
)
from supportagent.domain.hashing import canonical_json, payload_hash
from supportagent.domain.kinds import (
    CUSTOMER_FACING_KINDS,
    NEVER_EXECUTABLE_KINDS,
    ActionKind,
)
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.proposal_state import ProposalStatus
from supportagent.domain.trace import TraceContext

# --- constants ----------------------------------------------------------------

TENANT = "acme"
CASE_ID = "case_gate"
CREATOR = "user_creator"
APPROVER = "user_approver"
REQUESTER = "user_requester"
GRANTER = "user_granter"

PAST_TS = 1
FUTURE_TS = 9999999999

T0 = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)

PAYLOAD = {"note": "test note"}
PAYLOAD_JSON = canonical_json(PAYLOAD)
PAYLOAD_HASH = payload_hash(PAYLOAD)
OTHER_HASH = payload_hash({"note": "other"})

_NEVER_DOMAIN = frozenset(k.value for k in NEVER_EXECUTABLE_KINDS)
_CUSTOMER_DOMAIN = frozenset(k.value for k in CUSTOMER_FACING_KINDS)

# --- seeding (raw SQL; test setup only) ---------------------------------------


def _seed_base(db: Any) -> None:
    """The users + case every gate test shares."""
    with db.begin() as conn:
        for uid in (CREATOR, APPROVER, REQUESTER, GRANTER):
            conn.execute(
                sa.text(
                    "INSERT INTO users (id, tenant_id, email, name, created_ts) "
                    "VALUES (:id, :t, :e, :n, :c)"
                ),
                {"id": uid, "t": TENANT, "e": f"{uid}@acme.test", "n": uid, "c": 1},
            )
        conn.execute(
            sa.text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, "
                "opened_ts, last_alert_ts, trace_id) "
                "VALUES (:id, :t, :fp, 'alert', 'open', 1, 1, 'trace_seed')"
            ),
            {"id": CASE_ID, "t": TENANT, "fp": "fp-gate"},
        )


def _seed_proposal(  # noqa: PLR0913
    db: Any,
    *,
    id: str = "prop_1",
    status: str = "approved",
    created_by: str = "agent",
    payload_hash: str = PAYLOAD_HASH,
    kind: str = "work_note",
    scope: str = "scope:A",
    expires_ts: int | None = None,
) -> None:
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO proposals (id, tenant_id, case_id, action_kind, scope, "
                "payload_json, payload_hash, trace_id, status, created_by, "
                "created_at_ts, expires_ts) "
                "VALUES (:id, :t, :c, :k, :s, :pj, :ph, 'trace_seed', :st, :cb, 1, :et)"
            ),
            {
                "id": id,
                "t": TENANT,
                "c": CASE_ID,
                "k": kind,
                "s": scope,
                "pj": PAYLOAD_JSON,
                "ph": payload_hash,
                "st": status,
                "cb": created_by,
                "et": expires_ts,
            },
        )


def _seed_approval(  # noqa: PLR0913
    db: Any,
    *,
    id: str = "appr_1",
    proposal_id: str = "prop_1",
    status: str = "approved",
    approver: str = APPROVER,
    payload_hash: str = PAYLOAD_HASH,
) -> None:
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO approvals (id, tenant_id, proposal_id, approver_id, "
                "status, payload_hash, reason, approved_at_ts) "
                "VALUES (:id, :t, :p, :a, :st, :ph, NULL, 1)"
            ),
            {
                "id": id,
                "t": TENANT,
                "p": proposal_id,
                "a": approver,
                "st": status,
                "ph": payload_hash,
            },
        )


def _seed_grant(  # noqa: PLR0913
    db: Any,
    *,
    id: str = "grnt_1",
    status: str = "approved",
    valid_to_ts: int = FUTURE_TS,
    kind: str = "work_note",
    scope: str = "scope:A",
    requested_by: str = REQUESTER,
    granted_by: str | None = GRANTER,
) -> None:
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO policy_grants (id, tenant_id, action_kind, scope, status, "
                "requested_by, requested_at_ts, granted_by, granted_at_ts, valid_to_ts) "
                "VALUES (:id, :t, :k, :s, :st, :rb, 1, :gb, :gat, :vt)"
            ),
            {
                "id": id,
                "t": TENANT,
                "k": kind,
                "s": scope,
                "st": status,
                "rb": requested_by,
                "gb": granted_by,
                "gat": 1 if granted_by else None,
                "vt": valid_to_ts,
            },
        )


def _seed_plan(  # noqa: PLR0913
    db: Any,
    *,
    id: str = "plan_1",
    status: str = "approved",
    template_id: str = "tmpl_1",
    plan_hash: str = PAYLOAD_HASH,
    audience: str = "customer",
) -> None:
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO comms_plans (id, tenant_id, case_id, audience, channel, "
                "template_id, allowed_fields_json, cadence_minutes, plan_hash, "
                "proposal_id, approval_id, status) "
                "VALUES (:id, :t, :c, :au, 'email', :tmpl, '{}', NULL, :ph, "
                "'prop_1', 'appr_1', :st)"
            ),
            {
                "id": id,
                "t": TENANT,
                "c": CASE_ID,
                "au": audience,
                "tmpl": template_id,
                "ph": plan_hash,
                "st": status,
            },
        )


def _outbox_insert(  # noqa: PLR0913
    uow: Any,
    *,
    id: str = "obx_1",
    action_kind: str = "work_note",
    scope: str = "scope:A",
    payload_json: str = PAYLOAD_JSON,
    payload_hash: str = PAYLOAD_HASH,
    idempotency_key: str = "idem_1",
    approval_id: str | None = None,
    policy_grant_id: str | None = None,
    comms_plan_id: str | None = None,
) -> None:
    OutboxRepo(uow).insert(
        id=id,
        tenant_id=TENANT,
        case_id=CASE_ID,
        action_kind=action_kind,
        scope=scope,
        payload_json=payload_json,
        payload_hash=payload_hash,
        idempotency_key=idempotency_key,
        approval_id=approval_id,
        policy_grant_id=policy_grant_id,
        comms_plan_id=comms_plan_id,
        trace_id="trace_seed",
    )


def _scalar(db: Any, sql: str, **params: Any) -> Any:
    with db.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar_one()


# --- trigger / GWT tests ------------------------------------------------------


def test_gate_rejected_approval_blocked(db: Any, uow: Any) -> None:
    _seed_base(db)
    _seed_proposal(db)
    _seed_approval(db, status="rejected")
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(uow, approval_id="appr_1")


def test_gate_expired_approval_blocked(db: Any, uow: Any) -> None:
    _seed_base(db)
    _seed_proposal(db, status="expired")
    _seed_approval(db)
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(uow, approval_id="appr_1")


def test_gate_payload_hash_mismatch_blocked(db: Any, uow: Any) -> None:
    _seed_base(db)
    _seed_proposal(db)
    _seed_approval(db)
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(uow, approval_id="appr_1", payload_hash=OTHER_HASH)


def test_gate_grant_expired_or_out_of_scope_blocked(db: Any, uow: Any) -> None:
    _seed_base(db)
    # expired grant (valid_to_ts in the DB past) -> not enforceable
    _seed_grant(db, id="grnt_exp", valid_to_ts=PAST_TS)
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(uow, id="obx_1", policy_grant_id="grnt_exp")
    # unexpired grant but wrong scope -> not enforceable
    _seed_grant(db, id="grnt_scope", scope="scope:A")
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(uow, id="obx_2", scope="scope:B", policy_grant_id="grnt_scope")


def test_gate_grant_pending_not_enforceable(db: Any, uow: Any) -> None:
    _seed_base(db)
    _seed_grant(db, status="requested", granted_by=None)
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(uow, policy_grant_id="grnt_1")


def test_gate_approval_single_use(db: Any, uow: Any) -> None:
    _seed_base(db)
    _seed_proposal(db)
    _seed_approval(db)
    # first execution succeeds
    with uow:
        _outbox_insert(uow, id="obx_1", approval_id="appr_1", idempotency_key="idem_1")
        uow.commit()
    # second execution of the same approval is blocked
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(uow, id="obx_2", approval_id="appr_1", idempotency_key="idem_2")


def test_outbox_immutable_after_insert(db: Any, uow: Any) -> None:
    _seed_base(db)
    _seed_proposal(db)
    _seed_approval(db)
    with uow:
        _outbox_insert(uow, id="obx_1", approval_id="appr_1")
        uow.commit()
    # protected payload columns are immutable
    with (
        pytest.raises(sa.exc.DBAPIError, match="INV-01: outbox authorization/payload is immutable"),
        db.begin() as conn,
    ):
        conn.execute(sa.text("UPDATE outbox SET payload_json = '{}' WHERE id = 'obx_1'"))
    # the lifecycle column stays mutable
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE outbox SET status = 'claimed' WHERE id = 'obx_1'"))


def test_customer_comm_never_by_policy_grant(db: Any, uow: Any) -> None:
    _seed_base(db)
    _seed_grant(db, kind="customer_comment", scope="scope:C")
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(
            uow,
            action_kind="customer_comment",
            scope="scope:C",
            policy_grant_id="grnt_1",
        )


def test_comms_plan_revoked_blocked(db: Any, uow: Any) -> None:
    _seed_base(db)
    _seed_proposal(db)
    _seed_approval(db)
    _seed_plan(db, status="revoked", template_id="tmpl_1")
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(
            uow,
            action_kind="customer_comment",
            payload_json='{"template_id": "tmpl_1"}',
            comms_plan_id="plan_1",
        )


def test_create_page_never_executable(db: Any, uow: Any) -> None:
    _seed_base(db)
    _seed_proposal(db, kind="create_page")
    _seed_approval(db)
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(uow, action_kind="create_page", approval_id="appr_1")


def test_self_approval_blocked(db: Any) -> None:
    _seed_base(db)
    # approver == proposal creator -> trigger aborts
    _seed_proposal(db, created_by=CREATOR)
    with pytest.raises(sa.exc.DBAPIError, match="INV-01: self-approval"), db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO approvals (id, tenant_id, proposal_id, approver_id, "
                "status, payload_hash, approved_at_ts) "
                "VALUES ('appr_self', :t, 'prop_1', :a, 'approved', :ph, 1)"
            ),
            {"t": TENANT, "a": CREATOR, "ph": PAYLOAD_HASH},
        )
    # granted_by == requested_by on an approved grant -> CHECK fails
    _seed_grant(db, status="requested", granted_by=None, requested_by=REQUESTER)
    with pytest.raises(sa.exc.DBAPIError), db.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE policy_grants SET status = 'approved', granted_by = :g, "
                "granted_at_ts = 1, valid_to_ts = :vt WHERE id = 'grnt_1'"
            ),
            {"g": REQUESTER, "vt": FUTURE_TS},
        )


def _trigger_sql(db: Any) -> str:
    dialect = db.dialect.name
    with db.connect() as conn:
        if dialect == "sqlite":
            return str(
                conn.execute(
                    sa.text(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name = 'trg_outbox_inv01'"
                    )
                ).scalar_one()
            )
        return str(
            conn.execute(
                sa.text("SELECT prosrc FROM pg_proc WHERE proname = 'fn_outbox_inv01'")
            ).scalar_one()
        )


def _kind_lists(sql: str) -> list[frozenset[str]]:
    lists: list[frozenset[str]] = []
    for pattern in (r"IN\s*\(([^()]*)\)", r"ARRAY\[([^\]]*)\]"):
        for group in re.findall(pattern, sql):
            strings = frozenset(re.findall(r"'([a-z0-9_]+)'", group))
            if strings:
                lists.append(strings)
    return lists


def test_trigger_kind_lists_match_code(db: Any) -> None:
    lists = _kind_lists(_trigger_sql(db))
    never = [s for s in lists if "create_page" in s]
    customer = [s for s in lists if "watch_list_add_customer" in s]
    assert len(never) == 1
    assert never[0] == _NEVER_DOMAIN
    assert len(customer) == 1
    assert customer[0] == _CUSTOMER_DOMAIN


# --- behaviour (GateService) tests --------------------------------------------


def _catalog() -> Catalog:
    return Catalog(version="1", services=[ServiceEntry(name="payments")])


def _gate(db: Any, uow: Any, clock: Any, ids: Any, events: Any) -> GateService:
    return GateService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        catalog=_catalog(),
        ttl=timedelta(hours=1),
    )


def _spec(kind: ActionKind, *, service: str = "payments", scope: str = "scope:A") -> ActionSpec:
    return ActionSpec(
        kind=kind,
        target=TargetRef(service=service, system="servicenow", ref="ref-1"),
        scope=scope,
        payload=PAYLOAD,
    )


def _trace() -> TraceContext:
    return TraceContext.new(TENANT, "trace_gate", "span_gate")


def test_gate_submit_drops_unknown_service_target(
    db: Any, uow: Any, clock: Any, ids: Any, events: Any
) -> None:
    _seed_base(db)
    gate = _gate(db, uow, clock, ids, events)
    spec = _spec(ActionKind.WORK_NOTE, service="unknown_service")
    with pytest.raises(DomainValidationError):
        gate.submit(CASE_ID, spec, _trace())
    assert int(_scalar(db, "SELECT COUNT(*) FROM proposals")) == 0


def test_gate_submit_grant_path_auto_approves_internal_kind(
    db: Any, uow: Any, clock: Any, ids: Any, events: Any
) -> None:
    _seed_base(db)
    _seed_grant(db, kind="work_note", scope="scope:A")
    gate = _gate(db, uow, clock, ids, events)
    spec = _spec(ActionKind.WORK_NOTE, scope="scope:A")
    proposal = gate.submit(CASE_ID, spec, _trace())
    assert proposal.status is ProposalStatus.APPROVED
    assert int(_scalar(db, "SELECT COUNT(*) FROM approvals")) == 0


def test_gate_submit_customer_kind_never_grant_path(
    db: Any, uow: Any, clock: Any, ids: Any, events: Any
) -> None:
    _seed_base(db)
    _seed_grant(db, kind="customer_comment", scope="scope:B")
    gate = _gate(db, uow, clock, ids, events)
    spec = _spec(ActionKind.CUSTOMER_COMMENT, scope="scope:B")
    proposal = gate.submit(CASE_ID, spec, _trace())
    assert proposal.status is ProposalStatus.PENDING_APPROVAL


def test_gate_approve_stale_hash_conflict(
    db: Any, uow: Any, clock: Any, ids: Any, events: Any
) -> None:
    _seed_base(db)
    _seed_proposal(db, status="pending_approval")
    gate = _gate(db, uow, clock, ids, events)
    with pytest.raises(AuthorizationDenied):
        gate.approve("prop_1", APPROVER, seen_hash=OTHER_HASH, reason="ok")
    assert _scalar(db, "SELECT status FROM proposals WHERE id = 'prop_1'") == ("pending_approval")


def test_gate_approve_concurrent_second_fails(
    db: Any, uow: Any, clock: Any, ids: Any, events: Any
) -> None:
    _seed_base(db)
    _seed_proposal(db, status="pending_approval")
    gate = _gate(db, uow, clock, ids, events)
    gate.approve("prop_1", APPROVER, seen_hash=PAYLOAD_HASH, reason="ok")
    with pytest.raises(ConcurrencyConflict):
        gate.approve("prop_1", GRANTER, seen_hash=PAYLOAD_HASH, reason="ok")


def test_gate_expire_due_marks_expired(
    db: Any, uow: Any, clock: Any, ids: Any, events: Any
) -> None:
    _seed_base(db)
    _seed_proposal(db, id="prop_1", status="pending_approval", expires_ts=100)
    _seed_proposal(db, id="prop_2", status="pending_approval", expires_ts=100)
    gate = _gate(db, uow, clock, ids, events)
    count = gate.expire_due(now=T0)
    assert count == 2
    assert _scalar(db, "SELECT status FROM proposals WHERE id = 'prop_1'") == ("expired")
    assert _scalar(db, "SELECT status FROM proposals WHERE id = 'prop_2'") == ("expired")
    gate_events = [e for e in events.appended if e.kind.value == "gate"]
    assert len(gate_events) == 2


# --- ARCH §6.2/§6.3 conformance (PR review: plan path, idempotency, one path) --

_PLAN_SEND_JSON = '{"template_id": "tmpl_1"}'


def _seed_approved_plan(db: Any, **plan: Any) -> None:
    """An approved comms plan whose approval saw ``plan_hash`` (ARCH §6.3)."""
    _seed_base(db)
    _seed_proposal(db, kind="comms_plan")
    _seed_approval(db)
    _seed_plan(db, **plan)


def test_comms_plan_authorizes_matching_customer_send(db: Any, uow: Any) -> None:
    _seed_approved_plan(db)
    with uow:
        _outbox_insert(
            uow,
            action_kind="customer_comment",
            payload_json=_PLAN_SEND_JSON,
            comms_plan_id="plan_1",
        )
        uow.commit()
    assert _scalar(db, "SELECT COUNT(*) FROM outbox WHERE comms_plan_id = 'plan_1'") == 1


@pytest.mark.parametrize("kind", ["job_rerun", "ticket_update", "create_ticket_customer"])
def test_comms_plan_never_authorizes_other_kinds(db: Any, uow: Any, kind: str) -> None:
    """ARCH §6.2 path (3): plans authorize customer_comment/send_email_customer only."""
    _seed_approved_plan(db)
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(uow, action_kind=kind, payload_json=_PLAN_SEND_JSON, comms_plan_id="plan_1")


def test_comms_plan_template_mismatch_blocked(db: Any, uow: Any) -> None:
    _seed_approved_plan(db)
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(
            uow,
            action_kind="customer_comment",
            payload_json='{"template_id": "tmpl_other"}',
            comms_plan_id="plan_1",
        )


def test_comms_plan_edited_after_approval_blocked(db: Any, uow: Any) -> None:
    """ARCH §6.3: the approval's hash must equal plan_hash, so an edited plan is void."""
    _seed_approved_plan(db, plan_hash=OTHER_HASH)
    with uow, pytest.raises(AuthorizationDenied):
        _outbox_insert(
            uow,
            action_kind="customer_comment",
            payload_json=_PLAN_SEND_JSON,
            comms_plan_id="plan_1",
        )


def test_outbox_idempotency_key_unique(db: Any, uow: Any) -> None:
    """INV-04: one outbox row per idempotency key (ARCH §6.2 ``UNIQUE``)."""
    _seed_base(db)
    _seed_grant(db)
    with uow:
        _outbox_insert(uow, id="obx_1", policy_grant_id="grnt_1")
        uow.commit()
    with uow, pytest.raises(sa.exc.IntegrityError):
        _outbox_insert(uow, id="obx_2", policy_grant_id="grnt_1")


def test_outbox_exactly_one_authorization_path(db: Any, uow: Any) -> None:
    """ARCH §6.2: exactly one of approval / grant / plan backs a row."""
    _seed_base(db)
    _seed_proposal(db)
    _seed_approval(db)
    _seed_grant(db)
    with uow, pytest.raises(sa.exc.DBAPIError):
        _outbox_insert(uow, approval_id="appr_1", policy_grant_id="grnt_1")


def test_grant_statuses_and_config_requester_storable(db: Any) -> None:
    """ARCH §6.2/§6.4: 'rejected' is a grant status; requested_by may be 'config:<sha>'."""
    _seed_base(db)
    _seed_grant(db, status="rejected", requested_by="config:abc123", granted_by=None)
    assert _scalar(db, "SELECT status FROM policy_grants WHERE id = 'grnt_1'") == "rejected"


def test_gate_status_literals_match_domain() -> None:
    """03 §1: CHECK literal sets are generated from the domain enums."""
    from supportagent.adapters.db import tables  # noqa: PLC0415
    from supportagent.domain.model.comms import CommsPlanStatus  # noqa: PLC0415
    from supportagent.domain.model.grant import GrantStatus  # noqa: PLC0415

    assert set(tables.POLICY_GRANT_STATUSES) == {s.value for s in GrantStatus}
    assert set(tables.COMMS_PLAN_STATUSES) == {s.value for s in CommsPlanStatus}
    assert set(tables.OUTBOX_STATUSES) == {"pending", "claimed", "done", "failed"}
