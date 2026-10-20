"""Generic webhook rules (ARCH §7 inbound transport security, 02 §2; 11-wp WP-113).

**Signature.** ``X-SA-Signature: sha256=<hex>`` is HMAC-SHA256 under the
source's secret over ``<X-SA-Timestamp> + "." + <raw body>``, compared in
constant time. The timestamp must be within 5 minutes of now, and the
nonce (``X-SA-Nonce``) is single-use: the nonce store refuses a replay.

**Mapping.** Each configured source maps ``AlertEvent`` fields to paths in
the JSON body: ``$.a.b``, ``$.items[0].id`` (keys and list indexes only, no
filters or wildcards). ``external_id`` and ``subject`` are required; the
rest are optional. Every mapped value must be a string or a number;
an object or a list is an error.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.severity import Severity

WINDOW_S = 300
NONCE_TTL_S = 2 * WINDOW_S
_PATH = re.compile(r"^\$((?:\.[A-Za-z_][A-Za-z0-9_-]*|\[\d+\])*)$")
_STEP = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[(\d+)\]")
MAPPABLE = frozenset(
    {
        "external_id",
        "subject",
        "body",
        "resource",
        "alert_type",
        "error_signature",
        "severity_hint",
        "cmdb_ci",
    }
)
REQUIRED = frozenset({"external_id", "subject"})
_SEVERITY = {
    "1": "P1",
    "2": "P2",
    "3": "P3",
    "4": "P4",
    "critical": "P1",
    "high": "P2",
    "medium": "P3",
    "moderate": "P3",
    "low": "P4",
}

RejectReason = Literal[
    "unknown_source", "bad_signature", "stale_timestamp", "replayed_nonce", "bad_payload"
]


class WebhookRejected(Exception):  # noqa: N818 — names the outcome the event records
    def __init__(self, reason: RejectReason, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason: RejectReason = reason
        self.detail = detail


def validate_mapping(mapping: Mapping[str, str]) -> None:
    unknown = sorted(set(mapping) - MAPPABLE)
    if unknown:
        raise DomainValidationError(f"unmappable AlertEvent field(s): {', '.join(unknown)}")  # noqa: TRY003
    missing = sorted(REQUIRED - set(mapping))
    if missing:
        raise DomainValidationError(f"mapping needs {', '.join(missing)}")  # noqa: TRY003
    bad = sorted(path for path in mapping.values() if not _PATH.match(path))
    if bad:
        raise DomainValidationError(  # noqa: TRY003
            f"paths must look like $.a.b or $.items[0].c: {', '.join(bad)}"
        )


def extract(doc: object, path: str) -> object:
    """The value at ``path`` in ``doc``, or ``None`` when any step is missing."""
    match = _PATH.match(path)
    if match is None:
        raise DomainValidationError(f"bad path {path!r}")  # noqa: TRY003
    node = doc
    for key, index in _STEP.findall(match.group(1)):
        if key:
            if not isinstance(node, Mapping) or key not in node:
                return None
            node = node[key]
        else:
            if not isinstance(node, list) or int(index) >= len(node):
                return None
            node = node[int(index)]
    return node


def _text(field: str, value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise WebhookRejected("bad_payload", f"{field} must be a string or number")
    return str(value)


def map_alert(
    doc: object,
    mapping: Mapping[str, str],
    *,
    tenant_id: str,
    source: str,
    received_at: datetime,
) -> AlertEvent:
    values = {field: _text(field, extract(doc, path)) for field, path in mapping.items()}
    for field in REQUIRED:
        if not values.get(field):
            raise WebhookRejected("bad_payload", f"{field} is missing")
    raw_sev = (values.get("severity_hint") or "").strip()
    severity = _SEVERITY.get(raw_sev.lower(), raw_sev.upper()) if raw_sev else None
    if severity is not None and severity not in {s.value for s in Severity}:
        severity = None  # an unknown severity is a hint we ignore, not a rejection
    external_id = str(values["external_id"])
    try:
        return AlertEvent(
            tenant_id=tenant_id,
            source="webhook",
            origin="alert",
            external_id=external_id,
            table=None,
            ticket_number=None,
            received_at=received_at,
            subject=str(values["subject"])[:4000],
            body=(values.get("body") or "")[:65536],
            resource=values.get("resource"),
            alert_type=values.get("alert_type") or source,
            error_signature=values.get("error_signature"),
            severity_hint=Severity(severity) if severity else None,
            cmdb_ci=values.get("cmdb_ci"),
            raw_ref=f"webhook:{source}/{external_id}",
        )
    except ValueError as exc:
        raise WebhookRejected("bad_payload", str(exc)[:200]) from exc


def signature(secret: bytes, timestamp: str, body: bytes) -> str:
    """``sha256=<hex>`` over ``timestamp + "." + body`` (02 §2)."""
    digest = hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def check_signature(
    secret: bytes, *, timestamp: str, body: bytes, header: str, now: datetime
) -> int:
    """Raise ``WebhookRejected`` unless signed and fresh; the timestamp (epoch seconds)."""
    if not timestamp.isdigit():
        raise WebhookRejected("stale_timestamp", "X-SA-Timestamp must be epoch seconds")
    if not hmac.compare_digest(signature(secret, timestamp, body), header.strip()):
        raise WebhookRejected("bad_signature")
    ts = int(timestamp)
    if abs(now.timestamp() - ts) > WINDOW_S:
        raise WebhookRejected("stale_timestamp", f"outside the {WINDOW_S}s window")
    return ts


__all__ = [
    "MAPPABLE",
    "NONCE_TTL_S",
    "REQUIRED",
    "WINDOW_S",
    "RejectReason",
    "WebhookRejected",
    "check_signature",
    "extract",
    "map_alert",
    "signature",
    "validate_mapping",
]
