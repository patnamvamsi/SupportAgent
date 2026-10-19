"""INV-02: one unresolved case per (tenant, fingerprint) (WP-009, ARCH §4.1).

The enforcement is the partial unique index ``ux_cases_unresolved``
(``(tenant_id, fingerprint) WHERE status <> 'resolved'``, 03 §2.2) plus the
``ON CONFLICT`` upsert (``CaseRepo.upsert_by_fingerprint``) — not the
ordering of code. A resolved case falls out of the index, so the next
alert with the same fingerprint opens a new case. Human tickets carry
their ticket number in the fingerprint (01 §7.3) and never collapse.

The 5 test ids below are frozen (WP-009 DoD) — do not rename.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa

from supportagent.adapters.db.repos.cases import CaseRepo
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.domain.fingerprint import fingerprint
from supportagent.domain.model.alert import AlertEvent
from tests.builders import T0, a_ticket, an_alert

TENANT = "acme"
CONCURRENCY = 8


# --- helpers ----------------------------------------------------------------


def _alert_fp(**overrides: Any) -> str:
    return fingerprint(AlertEvent(**an_alert(**overrides)), ())


def _ticket_fp(ticket_number: str) -> str:
    return fingerprint(AlertEvent(**a_ticket(ticket_number=ticket_number)), ())


def _upsert(
    db: Any,
    *,
    case_id: str,
    fp: str,
    origin: str = "alert",
    now: datetime = T0,
) -> Any:
    """One upsert through the repo in its own unit of work (ARCH §4.1)."""
    with SqlUnitOfWork(db) as uow:
        result = CaseRepo(uow).upsert_by_fingerprint(
            case_id=case_id,
            tenant_id=TENANT,
            fingerprint=fp,
            origin=origin,
            trace_id="trace-inv02",
            now=now,
        )
        uow.commit()
    return result


def _rows(db: Any, fp: str) -> list[Any]:
    with db.connect() as conn:
        return list(
            conn.execute(
                sa.text(
                    "SELECT * FROM cases "
                    "WHERE tenant_id = :t AND fingerprint = :fp ORDER BY id"
                ),
                {"t": TENANT, "fp": fp},
            )
        )


# --- the frozen tests ---------------------------------------------------------


def test_dedup_single_case(db: Any) -> None:
    """Two alerts with the same fingerprint → one case; the second upsert
    reports created=False and bumps duplicate_count (ARCH §4.1)."""
    fp = _alert_fp(resource="payments-api", alert_type="error_rate")
    r1 = _upsert(db, case_id="case-inv02-s1", fp=fp)
    r2 = _upsert(db, case_id="case-inv02-s2", fp=fp, now=T0 + timedelta(seconds=60))

    assert r1.created is True and r1.duplicate_count == 0
    assert r2.created is False and r2.duplicate_count == 1

    rows = _rows(db, fp)
    assert len(rows) == 1
    assert rows[0]["id"] == "case-inv02-s1"
    assert rows[0]["status"] == "open"
    assert rows[0]["duplicate_count"] == 1
    assert rows[0]["last_alert_ts"] > rows[0]["opened_ts"]


def test_dedup_concurrent_no_second_case(db: Any) -> None:
    """WP-009 DoD: 50 repeated runs of 8 concurrent upserts of one new
    fingerprint — on both engines (``make invariants``) exactly one case
    may be created and the row must end at duplicate_count == 7."""
    for run in range(50):
        fp = _alert_fp(resource=f"payments-api-run{run}", alert_type="error_rate")
        barrier = threading.Barrier(CONCURRENCY)
        lock = threading.Lock()
        results: list[Any] = []

        def worker(i: int) -> None:
            barrier.wait()
            result = _upsert(db, case_id=f"case-inv02-c{run}-{i}", fp=fp)
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(CONCURRENCY)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(results) == CONCURRENCY
        assert sum(r.created for r in results) == 1  # exactly one winner
        assert max(r.duplicate_count for r in results) == CONCURRENCY - 1

        rows = _rows(db, fp)
        assert len(rows) == 1
        assert rows[0]["duplicate_count"] == CONCURRENCY - 1


def test_dedup_human_owned_no_second_case(db: Any) -> None:
    """The partial index covers every non-terminal state (03 §2.2): a
    ``human_owned`` case keeps absorbing duplicates — no second case."""
    fp = _alert_fp(resource="payments-api", alert_type="error_rate")
    r1 = _upsert(db, case_id="case-inv02-h1", fp=fp)

    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE cases SET status = 'human_owned' WHERE id = :i"),
            {"i": "case-inv02-h1"},
        )

    r2 = _upsert(db, case_id="case-inv02-h2", fp=fp, now=T0 + timedelta(seconds=60))

    assert r1.created is True and r1.duplicate_count == 0
    assert r2.created is False and r2.duplicate_count == 1

    rows = _rows(db, fp)
    assert len(rows) == 1
    assert rows[0]["status"] == "human_owned"
    assert rows[0]["duplicate_count"] == 1


def test_human_tickets_never_collapse(db: Any) -> None:
    """01 §7.3: the fingerprint includes the ticket number, so two human
    tickets are two cases; a re-ingested ticket dedups into its own case,
    never into another ticket's case."""
    fp_a = _ticket_fp("INC-2026-42")
    fp_b = _ticket_fp("INC-2026-43")
    assert fp_a != fp_b

    ra = _upsert(db, case_id="case-inv02-t1", fp=fp_a, origin="ticket")
    rb = _upsert(db, case_id="case-inv02-t2", fp=fp_b, origin="ticket")
    assert ra.created is True and rb.created is True

    rb2 = _upsert(
        db, case_id="case-inv02-t3", fp=fp_b, origin="ticket", now=T0 + timedelta(seconds=60)
    )
    assert rb2.created is False and rb2.duplicate_count == 1

    rows = _rows(db, fp_a) + _rows(db, fp_b)
    assert len(rows) == 2
    assert {row["id"] for row in rows} == {"case-inv02-t1", "case-inv02-t2"}


def test_dedup_resolved_case_opens_new(db: Any) -> None:
    """A resolved case falls out of the partial index: the same fingerprint
    opens a new case (linked later by the ``similar`` edge)."""
    fp = _alert_fp(resource="payments-api", alert_type="error_rate")
    r1 = _upsert(db, case_id="case-inv02-r1", fp=fp)

    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE cases SET status = 'resolved', resolved_ts = :r WHERE id = :i"),
            {"i": "case-inv02-r1", "r": int((T0 + timedelta(hours=1)).timestamp())},
        )

    r2 = _upsert(db, case_id="case-inv02-r2", fp=fp, now=T0 + timedelta(hours=2))

    assert r1.created is True and r1.duplicate_count == 0
    assert r2.created is True and r2.duplicate_count == 0

    rows = _rows(db, fp)
    assert len(rows) == 2
    by_id = {row["id"]: row for row in rows}
    assert by_id["case-inv02-r1"]["status"] == "resolved"
    assert by_id["case-inv02-r2"]["status"] == "open"
