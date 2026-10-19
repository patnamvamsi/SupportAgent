"""Result of the dedup upsert (ARCH §4.1, INV-02).

``created`` must be derived from ``duplicate_count = 0`` in the RETURNING
clause — SQLite's ``changes()`` cannot distinguish an insert from an
update (critique-v3, fixed in the ARCH §4.1 statement).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UpsertResult:
    """Outcome of ``upsert_by_fingerprint`` (01 §7.3).

    ``created`` is True only when this upsert inserted the row; otherwise
    the existing unresolved case absorbed the alert and its
    ``duplicate_count`` is returned.
    """

    case_id: str
    created: bool
    duplicate_count: int
