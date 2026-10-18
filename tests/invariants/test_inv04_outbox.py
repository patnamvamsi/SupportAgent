"""INV-04: at-least-once delivery, effectively-once effect (WP-006, ARCH §6.2).

The outbox executor (``app.outbox.OutboxExecutor``) must deliver each approved
action to the external sink exactly once *in effect*, even across crashes,
retries, and concurrent workers. These invariant tests pin the behavioural
contract against a real (fake) ServiceNow sink and a real DB (the INV-01
triggers are live):

* a journal action appends exactly one entry whose **last line** is the
  idempotency marker ``[sa:<key>]`` (01 §7.5);
* a crash after the external write is reconciled on replay (no double-send);
* a pre-existing marker is reconciled, not re-sent;
* a payload that no longer hashes to what was authorized aborts (INV-01) with
  no external write;
* retryable faults back off and are re-tried; non-retryable faults fail fast;
* the retry budget (5) is exhausted into a terminal ``failed`` + safe-default;
* two workers racing on one row: exactly one claims (rowcount CAS).

The 8 test ids below are frozen (WP-006 DoD) — do not rename.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
import sqlalchemy as sa

from supportagent.adapters.db.repos.outbox import OutboxRepo
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.app.handlers import HANDLERS, marker
from supportagent.app.outbox import (
    MAX_ATTEMPTS,
    Authorization,
    OutboxExecutor,
    backoff_s,
)
from supportagent.domain.errors import ExternalServiceError
from supportagent.domain.events import EventKind
from supportagent.domain.hashing import payload_hash
from supportagent.domain.idempotency import approval_key
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.trace import TraceContext
from supportagent.ports.tickets import JournalField, SnTable

TENANT = "acme"
CASE_ID = "case_inv04"
CREATOR = "user_creator"
APPROVER = "user_approver"
TABLE = SnTable.INCIDENT
SYS_ID = "sn123"

#: The journal payload these tests drive the executor with (01 §7.5: a
#: non-empty ``text`` string).
WORK_NOTE = {"text": "work note body"}


# --- seeding ----------------------------------------------------------------


def _seed_base(db: Any) -> None:
    """The users + case (with an external ServiceNow target) every test shares."""
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
                "VALUES (:id, :t, :fp, 'alert', 'open', :et, :eid, 1, 1, 'trace_seed')"
            ),
            {"id": CASE_ID, "t": TENANT, "fp": "fp-inv04", "et": TABLE.value, "eid": SYS_ID},
        )


def _seed_auth(db: Any, *, kind: str, payload: dict[str, Any]) -> str:
    """An approved proposal + approval that authorize exactly ``payload`` (INV-01)."""
    ph = payload_hash(payload)
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO proposals (id, tenant_id, case_id, action_kind, scope, "
                "payload_json, payload_hash, trace_id, status, created_by, "
                "created_at_ts, expires_ts) "
                "VALUES ('prop_1', :t, :c, :k, 'scope:A', :pj, :ph, 'trace_seed', "
                "'approved', 'agent', 1, NULL)"
            ),
            {
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
                "VALUES ('appr_1', :t, 'prop_1', :a, 'approved', :ph, NULL, 1)"
            ),
            {"t": TENANT, "a": APPROVER, "ph": ph},
        )
    return ph


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
    return TraceContext.new(TENANT, "trace_inv04", "span_inv04")


def _scalar(db: Any, sql: str, **params: Any) -> Any:
    with db.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar_one()


def _crash_at(point: str) -> Any:
    """A crash hook that raises when the executor reaches ``point``."""

    def hook(step: str) -> None:
        if step == point:
            raise RuntimeError(f"simulated crash at {step}")  # noqa: TRY003

    return hook


def _enqueue_work_note(ex: OutboxExecutor) -> Any:
    return ex.enqueue(
        Authorization(approval_id="appr_1"),
        _spec(ActionKind.WORK_NOTE, WORK_NOTE),
        CASE_ID,
        _trace(),
    )


# --- the 8 frozen invariant tests ------------------------------------------


def test_marker_appended_to_journal_text(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, events: Any, fake_sn: Any
) -> None:
    """A sent journal action appends exactly one entry; last line == marker."""
    _seed_base(db)
    _seed_auth(db, kind="work_note", payload=WORK_NOTE)
    ex = _executor(db, uow, clock, ids, events, fake_sn)
    row = _enqueue_work_note(ex)
    res = ex.run_once(clock.now())

    key = approval_key("appr_1")
    assert res.status == "sent"
    entries = fake_sn.journal_entries(TABLE, SYS_ID, JournalField.WORK_NOTES)
    assert len(entries) == 1
    assert entries[0].splitlines()[-1] == marker(key)
    # The external write is recorded on the row (INV-04 traceability).
    assert res.external_ref == "incident:snj0001"
    assert _scalar(db, "SELECT status FROM outbox WHERE id = :i", i=row.id) == "done"
    assert _scalar(db, "SELECT external_ref FROM outbox WHERE id = :i", i=row.id) == (
        "incident:snj0001"
    )


def test_executor_reconciles_existing_marker(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, events: Any, fake_sn: Any
) -> None:
    """A marker already present externally is reconciled, never re-sent."""
    _seed_base(db)
    _seed_auth(db, kind="work_note", payload=WORK_NOTE)
    ex = _executor(db, uow, clock, ids, events, fake_sn)
    row = _enqueue_work_note(ex)

    key = approval_key("appr_1")
    # Pre-seed the external state as if the send had already happened.
    ref = fake_sn.append_journal(
        TABLE, SYS_ID, JournalField.WORK_NOTES, f"work note body\n{marker(key)}"
    )
    res = ex.run_once(clock.now())

    assert res.status == "reconciled"
    # Exactly one entry — the executor did not append a second.
    assert len(fake_sn.journal_entries(TABLE, SYS_ID, JournalField.WORK_NOTES)) == 1
    assert res.external_ref == ref.as_text()
    assert _scalar(db, "SELECT status FROM outbox WHERE id = :i", i=row.id) == "done"


def test_idempotent_replay(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, events: Any, fake_sn: Any
) -> None:
    """A crash after the external write is replayed as a reconcile (no double-send)."""
    _seed_base(db)
    _seed_auth(db, kind="work_note", payload=WORK_NOTE)
    # First executor: crash right after the sink write (before mark_done/commit).
    ex_crash = _executor(db, uow, clock, ids, events, fake_sn, crash_hook=_crash_at("after_send"))
    row = _enqueue_work_note(ex_crash)
    with pytest.raises(RuntimeError, match="after_send"):
        ex_crash.run_once(clock.now())
    # Claim rolled back → still pending; the sink kept its one entry.
    assert _scalar(db, "SELECT status FROM outbox WHERE id = :i", i=row.id) == "pending"
    assert len(fake_sn.journal_entries(TABLE, SYS_ID, JournalField.WORK_NOTES)) == 1

    # A clean worker must reconcile, not re-send.
    ex_clean = _executor(db, uow, clock, ids, events, fake_sn)
    res = ex_clean.run_once(clock.now())
    assert res.status == "reconciled"
    assert _scalar(db, "SELECT status FROM outbox WHERE id = :i", i=row.id) == "done"
    # INV-04: exactly one journal entry across both runs (effectively-once).
    assert len(fake_sn.journal_entries(TABLE, SYS_ID, JournalField.WORK_NOTES)) == 1
    assert fake_sn.write_count == 1


def test_executor_rehash_mismatch_aborts(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, events: Any, fake_sn: Any
) -> None:
    """A payload that no longer hashes to what was authorized aborts (INV-01)."""
    _seed_base(db)
    _seed_auth(db, kind="work_note", payload=WORK_NOTE)
    ex = _executor(db, uow, clock, ids, events, fake_sn)
    row = _enqueue_work_note(ex)

    # Simulate post-insert corruption: drop the immutability trigger (which
    # normally prevents this) and tamper with the payload so it no longer
    # matches the authorized hash.
    if db.dialect.name == "sqlite":
        drop = "DROP TRIGGER IF EXISTS trg_outbox_immutable"
    else:
        drop = "DROP TRIGGER IF EXISTS trg_outbox_immutable ON outbox"
    with db.begin() as conn:
        conn.execute(sa.text(drop))
        conn.execute(
            sa.text('UPDATE outbox SET payload_json = \'{"text": "tampered"}\' WHERE id = :i'),
            {"i": row.id},
        )

    res = ex.run_once(clock.now())
    assert res.status == "failed"
    assert _scalar(db, "SELECT status FROM outbox WHERE id = :i", i=row.id) == "failed"
    assert _scalar(db, "SELECT attempts FROM outbox WHERE id = :i", i=row.id) == 1
    # No external write happened.
    assert fake_sn.write_count == 0
    assert EventKind.EXECUTOR_HASH_MISMATCH in events.kinds()


def test_executor_retries_retryable_with_backoff(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, events: Any, fake_sn: Any
) -> None:
    """A retryable fault backs off (exponential) and the next attempt succeeds."""
    _seed_base(db)
    _seed_auth(db, kind="work_note", payload=WORK_NOTE)
    ex = _executor(db, uow, clock, ids, events, fake_sn)
    row = _enqueue_work_note(ex)

    fake_sn.queue_fault(ExternalServiceError("sn 500", retryable=True))
    t0 = clock.now()
    res = ex.run_once(clock.now())
    assert res.status == "retried"
    assert _scalar(db, "SELECT status FROM outbox WHERE id = :i", i=row.id) == "pending"
    assert _scalar(db, "SELECT attempts FROM outbox WHERE id = :i", i=row.id) == 1
    expected_next = int(t0.timestamp()) + backoff_s(1)
    assert _scalar(db, "SELECT next_attempt_ts FROM outbox WHERE id = :i", i=row.id) == (
        expected_next
    )

    # Advance past the backoff; the retry now succeeds.
    clock.advance(backoff_s(1) + 1)
    res2 = ex.run_once(clock.now())
    assert res2.status == "sent"
    assert _scalar(db, "SELECT status FROM outbox WHERE id = :i", i=row.id) == "done"
    assert _scalar(db, "SELECT attempts FROM outbox WHERE id = :i", i=row.id) == 1
    assert len(fake_sn.journal_entries(TABLE, SYS_ID, JournalField.WORK_NOTES)) == 1


def test_executor_fails_non_retryable_immediately(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, events: Any, fake_sn: Any
) -> None:
    """A non-retryable fault fails the row immediately (no retry scheduling)."""
    _seed_base(db)
    _seed_auth(db, kind="work_note", payload=WORK_NOTE)
    ex = _executor(db, uow, clock, ids, events, fake_sn)
    row = _enqueue_work_note(ex)

    fake_sn.queue_fault(ExternalServiceError("sn 400", retryable=False))
    res = ex.run_once(clock.now())
    assert res.status == "failed"
    assert _scalar(db, "SELECT status FROM outbox WHERE id = :i", i=row.id) == "failed"
    assert _scalar(db, "SELECT attempts FROM outbox WHERE id = :i", i=row.id) == 1
    assert _scalar(db, "SELECT next_attempt_ts FROM outbox WHERE id = :i", i=row.id) is None
    assert fake_sn.write_count == 0
    # Safe-default: the active case is handed to a human.
    assert _scalar(db, "SELECT status FROM cases WHERE id = :i", i=CASE_ID) == "human_owned"


def test_executor_gives_up_after_5_attempts(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, events: Any, fake_sn: Any
) -> None:
    """Five retryable faults exhaust the budget → failed + safe-default."""
    _seed_base(db)
    _seed_auth(db, kind="work_note", payload=WORK_NOTE)
    ex = _executor(db, uow, clock, ids, events, fake_sn)
    row = _enqueue_work_note(ex)

    for _ in range(MAX_ATTEMPTS):
        fake_sn.queue_fault(ExternalServiceError("sn 500", retryable=True))

    res: Any = None
    for _ in range(MAX_ATTEMPTS):
        res = ex.run_once(clock.now())
        clock.advance(100)  # make any pending retry due
    assert res is not None
    assert res.status == "failed"
    assert _scalar(db, "SELECT status FROM outbox WHERE id = :i", i=row.id) == "failed"
    assert _scalar(db, "SELECT attempts FROM outbox WHERE id = :i", i=row.id) == MAX_ATTEMPTS
    assert _scalar(db, "SELECT status FROM cases WHERE id = :i", i=CASE_ID) == "human_owned"
    assert fake_sn.write_count == 0  # never actually sent


def test_executor_concurrent_claims_exclusive(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, events: Any, fake_sn: Any
) -> None:
    """Two workers racing on one row: exactly one claims and sends (rowcount CAS)."""
    _seed_base(db)
    _seed_auth(db, kind="work_note", payload=WORK_NOTE)
    ex1 = _executor(db, uow, clock, ids, events, fake_sn)
    row = _enqueue_work_note(ex1)
    ex2 = _executor(db, SqlUnitOfWork(db), clock, ids, events, fake_sn)

    results: list[str] = []
    barrier = threading.Barrier(2)

    def run(ex: OutboxExecutor) -> None:
        barrier.wait()
        results.append(ex.run_once(clock.now()).status)

    t1 = threading.Thread(target=run, args=(ex1,))
    t2 = threading.Thread(target=run, args=(ex2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one claims + sends; the other finds nothing due.
    assert sorted(results) == ["none", "sent"]
    assert len(fake_sn.journal_entries(TABLE, SYS_ID, JournalField.WORK_NOTES)) == 1
    assert _scalar(db, "SELECT status FROM outbox WHERE id = :i", i=row.id) == "done"
