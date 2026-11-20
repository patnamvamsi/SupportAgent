"""Alertmanager and Grafana Alerting payloads → ``AlertEvent`` (ADR-0022 §2, WP-213).

Both tools post the same webhook shape (Alertmanager's version 4, which
Grafana Alerting extends): one delivery carries a batch of alerts, each with
``status`` (``firing`` or ``resolved``), ``labels``, ``annotations``,
``startsAt`` and usually a ``fingerprint``. This preset maps every alert to
one ``AlertEvent``, so a deployment writes no JSONPath:

- ``external_id``: the alert's ``fingerprint``, or a digest of its labels;
- ``subject``: the ``summary`` annotation, else ``<alertname>: <resource>``;
- ``body``: the ``description`` annotation, the labels, ``startsAt`` and the
  source URL;
- ``resource``: the first label in the source's ``service_labels`` that the
  alert carries (the catalog resolves the service from it);
- ``alert_type``: ``alertname``; ``severity_hint``: the ``severity`` label;
  ``cmdb_ci``: a ``cmdb_ci`` label, when the alert carries one.

A resolved alert keeps ``alert_status="resolved"``: it is never triaged as a
new incident, and it never closes a case by itself.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime

from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.severity import Severity
from supportagent.domain.webhook import WebhookRejected

#: Labels that name the affected component, in order of preference.
DEFAULT_SERVICE_LABELS: tuple[str, ...] = (
    "service",
    "compose_service",
    "container_label_com_docker_compose_service",
    "container",
    "job",
)
MAX_ALERTS = 1000
_SEVERITY = {
    "critical": "P1",
    "page": "P1",
    "error": "P2",
    "high": "P2",
    "warning": "P3",
    "medium": "P3",
    "info": "P4",
    "low": "P4",
}


def _strings(value: object, what: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WebhookRejected("bad_payload", f"{what} must be an object")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise WebhookRejected("bad_payload", f"{what} must map strings to strings")
        out[key] = item
    return out


def _label_digest(labels: Mapping[str, str]) -> str:
    joined = "\x1f".join(f"{k}={labels[k]}" for k in sorted(labels))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def _body(description: str, labels: Mapping[str, str], starts_at: str, url: str, value: str) -> str:
    lines = [description] if description else []
    if value:
        lines.append(f"value: {value}")
    lines.append("labels: " + ", ".join(f"{k}={labels[k]}" for k in sorted(labels)))
    if starts_at:
        lines.append(f"starts at: {starts_at}")
    if url:
        lines.append(f"source: {url}")
    return "\n".join(lines)[:65536]


def _one(  # noqa: PLR0913 — one alert plus the delivery's context
    alert: object,
    common: Mapping[str, str],
    *,
    tenant_id: str,
    source: str,
    received_at: datetime,
    service_labels: Sequence[str],
) -> AlertEvent:
    if not isinstance(alert, Mapping):
        raise WebhookRejected("bad_payload", "each alert must be an object")
    status = alert.get("status")
    if status not in ("firing", "resolved"):
        raise WebhookRejected("bad_payload", "alert status must be firing or resolved")
    labels = {**common, **_strings(alert.get("labels"), "labels")}
    annotations = _strings(alert.get("annotations"), "annotations")
    alertname = labels.get("alertname") or "alert"
    resource = next((labels[k] for k in service_labels if labels.get(k)), None)
    raw_fp = alert.get("fingerprint")
    fp = raw_fp if isinstance(raw_fp, str) and raw_fp else _label_digest(labels)
    external_id = f"{alertname}/{fp}"[:255]
    summary = annotations.get("summary") or (f"{alertname}: {resource}" if resource else alertname)
    url = alert.get("generatorURL") or alert.get("dashboardURL") or ""
    starts = alert.get("startsAt") or ""
    value = alert.get("valueString") or ""
    sev = _SEVERITY.get(labels.get("severity", "").strip().lower())
    return AlertEvent(
        tenant_id=tenant_id,
        source="webhook",
        origin="alert",
        alert_status=status,
        external_id=external_id,
        table=None,
        ticket_number=None,
        received_at=received_at,
        subject=summary[:4000],
        body=_body(
            annotations.get("description", ""),
            labels,
            starts if isinstance(starts, str) else "",
            url if isinstance(url, str) else "",
            value if isinstance(value, str) else "",
        ),
        resource=resource,
        alert_type=alertname,
        error_signature=None,
        severity_hint=Severity(sev) if sev else None,
        cmdb_ci=labels.get("cmdb_ci"),
        raw_ref=f"webhook:{source}/{external_id}"[:512],
    )


def map_alertmanager(
    doc: object,
    *,
    tenant_id: str,
    source: str,
    received_at: datetime,
    service_labels: Sequence[str] = DEFAULT_SERVICE_LABELS,
) -> list[AlertEvent]:
    """One ``AlertEvent`` per alert in an Alertmanager or Grafana delivery."""
    if not isinstance(doc, Mapping):
        raise WebhookRejected("bad_payload", "the body must be a JSON object")
    alerts = doc.get("alerts")
    if not isinstance(alerts, list):
        raise WebhookRejected("bad_payload", "alerts must be a list")
    if len(alerts) > MAX_ALERTS:
        raise WebhookRejected("bad_payload", f"more than {MAX_ALERTS} alerts in one delivery")
    common = _strings(doc.get("commonLabels"), "commonLabels")
    try:
        return [
            _one(
                alert,
                common,
                tenant_id=tenant_id,
                source=source,
                received_at=received_at,
                service_labels=service_labels,
            )
            for alert in alerts
        ]
    except ValueError as exc:  # a field the AlertEvent model refuses
        raise WebhookRejected("bad_payload", str(exc)[:200]) from exc


__all__ = ["DEFAULT_SERVICE_LABELS", "MAX_ALERTS", "map_alertmanager"]
