"""WP-008: the ``TicketSource`` contract (10-wp L124-127; 02 §6).

The read port is pinned against **both** implementations — the in-memory
``FakeServiceNow`` (``fake``) and the real ``ServiceNow`` adapter driven over
its own HTTP surface by the ``FakeServiceNowHTTP`` ASGI fake (``http``) — so
the shared behaviour is asserted, not the per-impl detail.

The two retry tests (``429`` / ``5xx``) run only against the real adapter:
the retry policy lives in ``ServiceNowHTTP`` (02 §5), which the in-memory fake
has no HTTP surface to exercise. They queue raw 429/5xx responses and assert on
the recorded sleep seam (never actually waiting).
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise
from typing import Any

import pytest

from supportagent.domain.errors import ServiceNowError
from supportagent.ports.tickets import SnTable
from tests.builders import T0


def test_poll_returns_records_updated_in_same_second_as_cursor(sn_env: Any) -> None:
    """``poll`` is inclusive at the cursor (02 §6 ``since``): a record updated
    in the same second as ``since`` is returned; a strictly earlier one is not."""
    since = T0
    sn_env["seed"](
        SnTable.INCIDENT,
        "rec_same",
        number="INC100",
        short_description="same second",
        sys_updated_on=since,
        sys_updated_by="u1",
        sys_mod_count=1,
    )
    sn_env["seed"](
        SnTable.INCIDENT,
        "rec_earlier",
        number="INC101",
        short_description="earlier",
        sys_updated_on=since - timedelta(seconds=1),
        sys_updated_by="u1",
        sys_mod_count=1,
    )
    batch = sn_env["source"].poll(SnTable.INCIDENT, since=since, limit=10)
    ids = [r.sys_id for r in batch.records]
    assert "rec_same" in ids
    assert "rec_earlier" not in ids
    # an inclusive page whose max ts is the cursor itself
    assert batch.next_cursor == since


def test_poll_pagination_complete(sn_env: Any) -> None:
    """Paging forward by ``next_cursor`` yields every changed record exactly
    once, in ``(sys_updated_on, sys_id)`` order — even though the inclusive
    ``>=`` window re-fetches the boundary record on the next page (02 §6)."""
    limit = 2
    for i in range(5):
        sn_env["seed"](
            SnTable.INCIDENT,
            f"rec{i}",
            number=f"INC{200 + i}",
            short_description=f"record {i}",
            sys_updated_on=T0 + timedelta(seconds=i),  # distinct seconds, no ties
            sys_updated_by="u1",
            sys_mod_count=1,
        )

    seen_ids: list[str] = []
    cursors: list[Any] = []
    cursor = T0
    pages = 0
    while True:
        batch = sn_env["source"].poll(SnTable.INCIDENT, since=cursor, limit=limit)
        pages += 1
        assert pages < 10  # safety: no infinite paging loop
        cursors.append(batch.next_cursor)
        for rec in batch.records:
            if rec.sys_id not in seen_ids:  # dedupe the re-fetched boundary record
                seen_ids.append(rec.sys_id)
        if len(batch.records) < limit:  # a partial (final) page -> done
            break
        cursor = batch.next_cursor

    # every record, exactly once, in order
    assert seen_ids == [f"rec{i}" for i in range(5)]
    # the cursor advances monotonically and ends at the latest ts
    assert all(b >= a for a, b in pairwise(cursors))
    assert cursors[-1] == T0 + timedelta(seconds=4)


def test_history_returns_audit_and_journal_ordered(sn_env: Any) -> None:
    """``history`` merges ``sys_audit`` (checkpoint > ``after_mod_count``) with
    the ``sys_journal_field`` entries, ordered by time (02 §6)."""
    table = SnTable.INCIDENT
    sid = "recH"
    # three audit field edits at distinct times (single-digit checkpoints)
    sn_env["seed_audit"](
        table, sid, user="u1", ts=T0, fieldname="state", oldvalue=None, newvalue="1", checkpoint=1
    )
    sn_env["seed_audit"](
        table,
        sid,
        user="u2",
        ts=T0 + timedelta(seconds=2),
        fieldname="priority",
        oldvalue=None,
        newvalue="1",
        checkpoint=2,
    )
    sn_env["seed_audit"](
        table,
        sid,
        user="u3",
        ts=T0 + timedelta(seconds=4),
        fieldname="assigned_to",
        oldvalue=None,
        newvalue="u3",
        checkpoint=3,
    )
    # two journal notes interleaved between the audit edits
    sn_env["seed_journal"](
        table,
        sid,
        user="u1",
        ts=T0 + timedelta(seconds=1),
        element="work_notes",
        value="first note",
    )
    sn_env["seed_journal"](
        table,
        sid,
        user="u1",
        ts=T0 + timedelta(seconds=3),
        element="work_notes",
        value="second note",
    )

    # after_mod_count=0 -> all five, interleaved by ts
    all_changes = sn_env["source"].history(table, sid, after_mod_count=0)
    assert [c.ts for c in all_changes] == [
        T0,
        T0 + timedelta(seconds=1),
        T0 + timedelta(seconds=2),
        T0 + timedelta(seconds=3),
        T0 + timedelta(seconds=4),
    ]
    assert [c.source for c in all_changes] == ["audit", "journal", "audit", "journal", "audit"]

    # after_mod_count=2 -> only the checkpoint-3 audit + both journal notes
    tail = sn_env["source"].history(table, sid, after_mod_count=2)
    assert [c.ts for c in tail] == [
        T0 + timedelta(seconds=1),
        T0 + timedelta(seconds=3),
        T0 + timedelta(seconds=4),
    ]
    assert [c.source for c in tail] == ["journal", "journal", "audit"]


def test_429_retry_after_honoured(sn_real: Any) -> None:
    """A ``429`` is retried and the ``Retry-After`` header value is used as the
    sole sleep (02 §5) — asserted on the recorded sleep seam, not wall time."""
    stack = sn_real
    stack["fake_http"].seed_record(
        SnTable.INCIDENT,
        "rec429",
        number="INC100",
        short_description="throttled",
        sys_updated_on=T0,
        sys_updated_by="u1",
        sys_mod_count=1,
    )
    stack["fake_http"].queue_status(429, headers={"Retry-After": "7"})
    batch = stack["facade"].poll(SnTable.INCIDENT, since=T0, limit=10)
    assert [r.sys_id for r in batch.records] == ["rec429"]
    # exactly one retry, sleeping exactly the advertised 7s (recorded, not waited)
    assert stack["sleeps"] == [7.0]


def test_5xx_retried_then_error(sn_real: Any) -> None:
    """Repeated ``5xx`` exhaust the retry budget (``max_tries=5``): the client
    backs off ``0.5s x 2^n`` for the non-final attempts, then raises a
    retryable ``ServiceNowError`` (02 §5)."""
    stack = sn_real
    stack["fake_http"].seed_record(
        SnTable.INCIDENT,
        "rec5xx",
        number="INC100",
        short_description="down",
        sys_updated_on=T0,
        sys_updated_by="u1",
        sys_mod_count=1,
    )
    for _ in range(5):  # one failure per attempt (max_tries = 5)
        stack["fake_http"].queue_status(500)
    with pytest.raises(ServiceNowError) as excinfo:
        stack["facade"].poll(SnTable.INCIDENT, since=T0, limit=10)
    assert excinfo.value.retryable is True
    # 0.5s x 2^n for the four non-final attempts; the final one fails fast
    assert stack["sleeps"] == [0.5, 1.0, 2.0, 4.0]


def test_poll_offset_pages_same_second_ties(sn_env: Any) -> None:
    """02 §6 ``sysparm_offset``: paging by offset from a fixed ``since`` reaches
    every record even when more than ``limit`` share one second."""
    for i in range(5):
        sn_env["seed"](
            SnTable.INCIDENT,
            f"tie{i}",
            number=f"INC{300 + i}",
            short_description=f"tie {i}",
            sys_updated_on=T0,  # all in the same second
            sys_updated_by="u1",
            sys_mod_count=1,
        )
    ids: list[str] = []
    offset = 0
    while True:
        batch = sn_env["source"].poll(SnTable.INCIDENT, since=T0, limit=2, offset=offset)
        ids.extend(r.sys_id for r in batch.records)
        if len(batch.records) < 2:
            break
        offset += 2
    assert ids == [f"tie{i}" for i in range(5)]


def test_history_since_filters_journal(sn_env: Any) -> None:
    """02 §6: journal history is limited to ``sys_created_on >= since``."""
    table, sid = SnTable.INCIDENT, "rec_hist_since"
    for offset_s, value in ((0, "old note"), (60, "new note")):
        sn_env["seed_journal"](
            table,
            sid,
            user="u1",
            ts=T0 + timedelta(seconds=offset_s),
            element="work_notes",
            value=value,
        )
    changes = sn_env["source"].history(
        table, sid, after_mod_count=0, since=T0 + timedelta(seconds=30)
    )
    assert [c.newvalue for c in changes if c.source == "journal"] == ["new note"]
