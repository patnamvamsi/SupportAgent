"""WP-008: the ``TicketSink`` contract (10-wp L124-127; 01 §7.5).

The write port is pinned against **both** implementations — the in-memory
``FakeServiceNow`` (``fake``) and the real ``ServiceNow`` adapter over the
``FakeServiceNowHTTP`` ASGI fake (``http``). Per the plan we assert the shared
behaviour (an ``ExternalRef`` is returned, the state is recorded, re-applying
is a no-op) — **not** the per-impl ``ref`` shape, which legitimately differs.

The record is always seeded first: the real sink read-compares against the
external record (404 if absent), whereas the in-memory fake keys its sink state
independently — seeding is a harmless no-op for it.
"""

from __future__ import annotations

from typing import Any

from supportagent.domain.model.external import ExternalRef
from supportagent.ports.tickets import NoteVisibility, RecordType
from tests.builders import T0


def _seed(env: Any, external_id: str, **row: Any) -> None:
    """Seed the record (both impls accept a ``SnTable`` for the table)."""
    env["seed"](
        RecordType.INCIDENT,
        external_id,
        number="INC100",
        short_description=external_id,
        updated_at=T0,
        updated_by="u1",
        revision=1,
        **row,
    )


def test_append_journal_returns_ref(sn_env: Any) -> None:
    """``append_journal`` returns an ``ExternalRef`` and the text is recorded so
    reconciliation (INV-04) can read it back via ``find_marker``."""
    env = sn_env
    _seed(env, "recA")
    marker = "[sa:contract_append_ref]"
    text = f"Investigated the issue.\n{marker}"
    ref = env["sink"].append_journal(RecordType.INCIDENT, "recA", NoteVisibility.INTERNAL, text)
    # shared contract: an ExternalRef is returned (shape is impl-specific)
    assert isinstance(ref, ExternalRef)
    assert ref.ref
    # ... and the appended text is externally readable, carrying the marker
    assert env["sink"].find_marker(RecordType.INCIDENT, "recA", marker) is not None


def test_find_marker_finds_appended_marker(sn_env: Any) -> None:
    """``find_marker`` returns a ref for a marker that was applied and ``None``
    for one that was never applied (INV-04 reconciliation read)."""
    env = sn_env
    _seed(env, "recF")
    marker = "[sa:contract_marker]"
    env["sink"].append_journal(
        RecordType.INCIDENT, "recF", NoteVisibility.INTERNAL, f"Note.\n{marker}"
    )
    # the applied marker is found
    found = env["sink"].find_marker(RecordType.INCIDENT, "recF", marker)
    assert found is not None
    assert isinstance(found, ExternalRef)
    # a marker that was never applied is not found
    assert env["sink"].find_marker(RecordType.INCIDENT, "recF", "[sa:does_not_exist_999]") is None


def test_find_marker_needs_the_last_line(sn_env: Any) -> None:
    """BL-003 / 01 §7.5: a marker quoted inside someone's note doesn't reconcile a send."""
    env = sn_env
    _seed(env, "recQ")
    marker = "[sa:contract_quoted]"
    env["sink"].append_journal(
        RecordType.INCIDENT, "recQ", NoteVisibility.INTERNAL, f"Saw {marker} earlier.\nThanks."
    )
    assert env["sink"].find_marker(RecordType.INCIDENT, "recQ", marker) is None
    env["sink"].append_journal(
        RecordType.INCIDENT, "recQ", NoteVisibility.INTERNAL, f"Agent note.\n{marker}\n"
    )
    assert env["sink"].find_marker(RecordType.INCIDENT, "recQ", marker) is not None


def test_update_fields_idempotent(sn_env: Any) -> None:
    """``update_fields`` reads then compares (01 §7.5): the first call writes
    once; an identical re-apply is a no-op (no second write), yet the value
    holds. We assert the write count and the value — not the ``ref`` shape."""
    env = sn_env
    _seed(env, "recU", state="0")
    sink = env["sink"]

    first = sink.update_fields(RecordType.INCIDENT, "recU", {"state": "1"})
    assert isinstance(first, ExternalRef)
    assert env["write_count"]() == 1  # first call writes exactly once
    assert env["read_field"](RecordType.INCIDENT, "recU", "state") == "1"

    second = sink.update_fields(RecordType.INCIDENT, "recU", {"state": "1"})
    assert isinstance(second, ExternalRef)
    # identical re-apply is a no-op: no additional write
    assert env["write_count"]() == 1
    assert env["read_field"](RecordType.INCIDENT, "recU", "state") == "1"
