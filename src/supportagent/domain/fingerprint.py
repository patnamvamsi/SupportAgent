"""Case fingerprint (01 §7.3): the stable identity behind dedup (INV-02).

Tickets: identity is (source, ticket_number) — the ticket number is what
keeps human tickets from ever collapsing into each other. Alerts: identity
is (source, resource, alert_type, error_signature), each part normalized
(01 §7.2) so UUID/timestamp/IP substitutions do not split one incident.

The digest is 40 hex chars (01 §7.3).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.normalize import normalize


def _n(value: str | None, consts: Sequence[str]) -> str:
    """Normalized part, or ``""`` when absent (01 §7.3 ``_n``)."""
    return "" if value is None else normalize(value, consts)


def _req(value: str | None) -> str:
    """Required part (01 §7.3 ``_req``); raise, never assert (bandit B101)."""
    if value is None:
        raise ValueError("ticket fingerprint requires ticket_number (01 §7.3)")  # noqa: TRY003
    return value


def fingerprint(evt: AlertEvent, consts: Sequence[str] = ()) -> str:
    """Stable case identity for ``evt`` (01 §7.3)."""
    if evt.origin == "ticket":
        parts = ["ticket", evt.source, _req(evt.ticket_number)]
    else:
        parts = [
            "alert",
            evt.source,
            _n(evt.resource, consts),
            _n(evt.alert_type, consts),
            _n(evt.error_signature, consts),
        ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:40]
