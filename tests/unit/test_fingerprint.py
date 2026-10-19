"""``fingerprint`` (01 §7.3) — stable identity for dedup (INV-02).

Tickets: the fingerprint is (origin, source, ticket_number) — the ticket
number is what makes human tickets never collapse. Alerts: the fingerprint
is (origin, source, resource, alert_type, error_signature), each part
normalized, so UUID, timestamp and IP substitutions must not split one
incident into many cases.
"""

from __future__ import annotations

import re

from hypothesis import given
from hypothesis import strategies as st

from supportagent.domain.fingerprint import fingerprint
from supportagent.domain.model.alert import AlertEvent
from tests.builders import a_ticket, an_alert

_FP_SHAPE = re.compile(r"^[0-9a-f]{40}$")


def test_fingerprint_ticket_uses_number() -> None:
    """01 §7.3: same ticket number → same identity whatever the text;
    different ticket numbers → different cases (never collapse)."""
    fp_inc42 = fingerprint(AlertEvent(**a_ticket()), ())
    fp_inc43 = fingerprint(AlertEvent(**a_ticket(ticket_number="INC-2026-43")), ())
    fp_inc42_reingested = fingerprint(
        AlertEvent(**a_ticket(ticket_number="INC-2026-42", subject="entirely different text")),
        (),
    )

    assert _FP_SHAPE.fullmatch(fp_inc42)  # 40 hex chars (01 §7.3)
    assert fp_inc42 == fp_inc42_reingested
    assert fp_inc42 != fp_inc43


_UUID = st.from_regex(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", fullmatch=True
)
_TS = st.from_regex(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", fullmatch=True)
_IP = st.from_regex(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", fullmatch=True)


@given(uuid1=_UUID, uuid2=_UUID, ts1=_TS, ts2=_TS, ip1=_IP, ip2=_IP)
def test_fingerprint_alert_ignores_uuid_ts_ip(
    uuid1: str, uuid2: str, ts1: str, ts2: str, ip1: str, ip2: str
) -> None:
    """01 §7.2/§7.3 property: substituting UUIDs, timestamps and IPs in an
    alert's identifying fields must not change its fingerprint."""

    def alert(uuid: str, ts: str, ip: str) -> AlertEvent:
        return AlertEvent(
            **an_alert(
                resource=f"payments-{ip}",
                alert_type="error_rate",
                error_signature=f"5xx:{ip} at {ts} id={uuid}",
            )
        )

    assert fingerprint(alert(uuid1, ts1, ip1), ()) == fingerprint(alert(uuid2, ts2, ip2), ())
