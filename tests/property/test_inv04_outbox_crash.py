"""INV-04 DoD (WP-006): effectively-once across 100 randomized crash points.

The outbox executor must deliver each approved action to the external sink
exactly once *in effect*, even across crashes (01 §7.5, ARCH §6.2). This
property test pins the WP-006 DoD (10-wp):

    ``FakeServiceNow`` sees exactly one journal entry per approved action
    across 100 randomized crash points (property test with crash injection
    between the executor's steps).

For each of 100 randomized (crash point, journal kind) scenarios, the worker
runs ``run_once`` once and dies at the crash point. A crash before the claim
commits rolls it back (the row returns to ``pending``); a later crash leaves
the row ``claimed`` until the lease expires. Any external sink write persists
(the INV-04 replay case). A clean worker then drains the queue. INV-04
requires that the sink ends with **exactly one** journal entry for the approved
action — its last line the idempotency marker — and the outbox row ``done``:
either the replay reconciled the pre-crash write (no double-send) or, for
pre-write crash points, sent it exactly once.

The crash points are the executor's five step boundaries (``after_claim``,
``after_rehash``, ``after_reconcile``, ``after_send``, ``after_mark_done``);
the kinds are the two journal kinds (``work_note``, ``customer_comment``). A
fixed RNG seed makes the 100 scenarios reproducible, and a distinct approval id
per scenario keeps the outbox's ``UNIQUE`` constraints (the approval-path key
derives only from the approval id) from colliding.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
import sqlalchemy as sa

from supportagent.adapters.db.repos.outbox import OutboxRepo
from supportagent.adapters.fakes.servicenow import FakeServiceNow
from supportagent.app.handlers import HANDLERS, JOURNAL_KINDS, marker
from supportagent.app.outbox import DEFAULT_LEASE_S, Authorization, OutboxExecutor
from supportagent.domain.hashing import payload_hash
from supportagent.domain.idempotency import approval_key
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.trace import TraceContext
from supportagent.ports.tickets import NoteVisibility, RecordType

TENANT = "acme"
CASE_ID = "case_inv04_dod"
CREATOR = "user_creator"
APPROVER = "user_approver"
TABLE = RecordType.INCIDENT
SYS_ID = "sn123"

#: The executor's five step boundaries (the crash points, 01 §7.5).
CRASH_POINTS: tuple[str, ...] = (
    "after_claim",
    "after_rehash",
    "after_reconcile",
    "after_send",
    "after_mark_done",
)

#: The WP-006 DoD: exactly 100 randomized crash points.
NUM_CRASH_POINTS = 100

#: A fixed seed so the 100 randomized scenarios are reproducible.
DOD_SEED = 20261020

#: The journal kinds the DoD drives (01 §7.5: work_note + customer_comment).
JOURNAL_KINDS_TUPLE: tuple[ActionKind, ...] = tuple(sorted(JOURNAL_KINDS, key=lambda k: k.value))


# --- seeding ----------------------------------------------------------------


def _seed_base(db: Any) -> None:
    """The users + case (with an external ServiceNow target) every scenario shares."""
    with db.begin() as conn:
        for uid in (CREATOR, APPROVER):
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
                "external_table, external_id, opened_ts, last_alert_ts, trace_id) "
                "VALUES (:id, :t, :fp, 'alert', 'open', :et, :eid, 1, 1, 'trace_dod')"
            ),
            {"id": CASE_ID, "t": TENANT, "fp": "fp-dod", "et": TABLE.value, "eid": SYS_ID},
        )


def _seed_auth(db: Any, *, approval_id: str, kind: str, payload: dict[str, Any]) -> None:
    """A unique approved proposal + approval that authorize ``payload`` (INV-01).

    Each DoD scenario uses a distinct ``approval_id`` so the outbox's
    ``UNIQUE(idempotency_key)`` / ``UNIQUE(approval_id)`` constraints (the
    approval-path key derives only from the approval id) never collide.
    """
    proposal_id = f"prop_{approval_id}"
    ph = payload_hash(payload)
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO proposals (id, tenant_id, case_id, action_kind, scope, "
                "payload_json, payload_hash, trace_id, status, created_by, "
                "created_at_ts, expires_ts) "
                "VALUES (:pid, :t, :c, :k, 'scope:A', :pj, :ph, 'trace_dod', "
                "'approved', 'agent', 1, NULL)"
            ),
            {
                "pid": proposal_id,
                "t": TENANT,
                "c": CASE_ID,
                "k": kind,
                "pj": str(payload),
                "ph": ph,
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO approvals (id, tenant_id, proposal_id, approver_id, "
                "status, payload_hash, reason, approved_at_ts) "
                "VALUES (:aid, :t, :pid, :a, 'approved', :ph, NULL, 1)"
            ),
            {"aid": approval_id, "t": TENANT, "pid": proposal_id, "a": APPROVER, "ph": ph},
        )


# --- helpers ----------------------------------------------------------------


def _executor(  # noqa: PLR0913, PLR0917
    db: Any,
    uow: Any,
    clock: Any,
    ids: Any,
    events: Any,
    fake_sn: Any,
    *,
    crash_hook: Any = None,
) -> OutboxExecutor:
    return OutboxExecutor(
        uow=uow,
        repo=OutboxRepo(uow),
        clock=clock,
        ids=ids,
        events=events,
        sink=fake_sn,
        handlers=HANDLERS,
        crash_hook=crash_hook,
    )


def _spec(kind: ActionKind, payload: dict[str, Any]) -> ActionSpec:
    return ActionSpec(
        kind=kind,
        target=TargetRef(service="payments", system="servicenow", ref="ref-1"),
        scope="scope:A",
        payload=payload,
    )


def _trace() -> TraceContext:
    return TraceContext.new(TENANT, "trace_dod", "span_dod")


def _crash_at(point: str) -> Any:
    """A crash hook that raises when the executor reaches ``point``."""

    def hook(step: str) -> None:
        if step == point:
            raise RuntimeError(f"simulated crash at {step}")  # noqa: TRY003

    return hook


def _scalar(db: Any, sql: str, **params: Any) -> Any:
    with db.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar_one()


def _field_for(kind: ActionKind) -> NoteVisibility:
    """The journal field a journal kind appends to (01 §7.5)."""
    if kind is ActionKind.WORK_NOTE:
        return NoteVisibility.INTERNAL
    if kind is ActionKind.CUSTOMER_COMMENT:
        return NoteVisibility.CUSTOMER
    msg = f"not a journal kind: {kind!r}"
    raise ValueError(msg)


# --- the DoD property -------------------------------------------------------


def test_dod_exactly_one_journal_entry_per_approved_action(
    db: Any, uow: Any, clock: Any, ids: Any, events: Any
) -> None:
    """DoD: exactly one journal entry per approved action across 100 crashes.

    For 100 randomized (crash point, journal kind) scenarios, the worker runs
    ``run_once`` once and dies at the crash point (any open unit of work rolls
    back, any external sink write persists). Once the lease has expired, a
    clean worker drains the queue. INV-04 requires the sink to end with exactly
    one journal entry for the approved action (last line the marker) and the
    outbox row ``done``.
    """
    _seed_base(db)
    rng = random.Random(DOD_SEED)  # noqa: S311  # non-crypto: reproducible scenario order
    for i in range(NUM_CRASH_POINTS):
        point = rng.choice(CRASH_POINTS)
        kind = rng.choice(JOURNAL_KINDS_TUPLE)
        approval_id = f"appr_dod_{i:03d}"
        payload = {"text": f"dod note {i}"}

        # A fresh approval (frees the UNIQUE key) + a fresh sink (no reset API).
        _seed_auth(db, approval_id=approval_id, kind=kind.value, payload=payload)
        fake_sn = FakeServiceNow()

        ex_crash = _executor(db, uow, clock, ids, events, fake_sn, crash_hook=_crash_at(point))
        row = ex_crash.enqueue(
            Authorization(approval_id=approval_id),
            _spec(kind, payload),
            CASE_ID,
            _trace(),
        )
        # The worker runs once and dies at the randomized point.
        with pytest.raises(RuntimeError, match=point):
            ex_crash.run_once(clock.now())

        # A clean worker drains the queue to empty, after the dead worker's
        # claim (committed for post-claim crash points) has gone stale.
        clock.advance(DEFAULT_LEASE_S)
        ex_clean = _executor(db, uow, clock, ids, events, fake_sn)
        while True:
            res = ex_clean.run_once(clock.now())
            if res.status == "none":
                break

        # INV-04 / DoD: exactly one journal entry for this approved action.
        field = _field_for(kind)
        entries = fake_sn.journal_entries(TABLE, SYS_ID, field)
        assert len(entries) == 1, f"crash={point} kind={kind.value}"
        assert entries[0].splitlines()[-1] == marker(approval_key(approval_id))
        # The outbox row is done (delivered or reconciled, not stuck).
        assert _scalar(db, "SELECT status FROM outbox WHERE id = :i", i=row.id) == "done"
