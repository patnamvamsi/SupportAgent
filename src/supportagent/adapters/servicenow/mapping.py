"""ServiceNow JSON → domain mapping (02 §6).

Pure functions: no I/O, no vendor types. ``row_to_record`` turns one raw
``/api/now/table`` row into a ``TicketRecord``; ``audit_row_to_change`` /
``journal_row_to_change`` turn ``sys_audit`` / ``sys_journal_field`` rows into
``ChangeRecord``s; ``record_to_alert`` turns a new ``TicketRecord`` into the
normalized ``AlertEvent`` (origin, severity_hint, subject/body/resource per
02 §6). ``sys_mod_count`` arrives as a string from the API and is coerced to
``int`` here — the domain type is strict (01 §4).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from supportagent.adapters.servicenow.errors import ServiceNowError
from supportagent.adapters.servicenow.semantics import severity_from_priority
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.ticket import ChangeRecord, TicketRecord

# 02 §6: the AlertEvent field → ServiceNow field mapping. There is no
# ``field_map`` in the config schema, so the defaults live here and the
# adapter constructor exposes an override (02 §6).
DEFAULT_FIELD_MAP: Mapping[str, str] = {
    "subject": "short_description",
    "body": "description",
    # 02 §6: resource ← cmdb_ci.name (BL-045), which catalog `resources` list.
    "resource": "cmdb_ci.name",
}


def _opt_str(value: Any) -> str | None:  # noqa: ANN401
    """A ServiceNow display/ID value: ``None`` for null/empty, else ``str``.

    BL-030: a reference field the instance rendered as ``{"link", "value"}``
    (a request without ``sysparm_exclude_reference_link``) yields its ``value``.
    """
    if isinstance(value, Mapping):
        value = value.get("value")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_aware_utc(value: Any) -> datetime:  # noqa: ANN401
    """A ServiceNow datetime (string or ``datetime``) → aware UTC (01 §4)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ServiceNowError(f"unsupported datetime value: {value!r}")  # noqa: TRY003
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_int(value: Any) -> int:  # noqa: ANN401
    """``sys_mod_count`` / ``record_checkpoint``: the API sends a string."""
    if isinstance(value, bool):  # bool is an int subclass — reject it
        raise ServiceNowError(f"unsupported integer value: {value!r}")  # noqa: TRY003
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value.strip())
    raise ServiceNowError(f"unsupported integer value: {value!r}")  # noqa: TRY003


def row_to_record(table: str, row: Mapping[str, Any]) -> TicketRecord:
    """One ``/api/now/table`` row → ``TicketRecord`` (02 §6)."""
    return TicketRecord(
        table=table,
        external_id=str(row["sys_id"]),
        number=_opt_str(row.get("number")),
        short_description=_opt_str(row.get("short_description")),
        description=_opt_str(row.get("description")),
        priority=_opt_str(row.get("priority")),
        state=_opt_str(row.get("state")),
        assigned_to=_opt_str(row.get("assigned_to")),
        assignment_group=_opt_str(row.get("assignment_group")),
        cmdb_ci=_opt_str(row.get("cmdb_ci")),
        cmdb_ci_name=_opt_str(row.get("cmdb_ci.name")),
        created_by=_opt_str(row.get("sys_created_by")),
        caller_id=_opt_str(row.get("caller_id")),
        alert_source=_opt_str(row.get("u_alert_source")),
        updated_at=_to_aware_utc(row["sys_updated_on"]),
        updated_by=str(row.get("sys_updated_by") or "unknown"),
        revision=_to_int(row["sys_mod_count"]),
    )


def audit_row_to_change(row: Mapping[str, Any]) -> ChangeRecord:
    """One ``sys_audit`` row → ``ChangeRecord`` (02 §6 ``history``)."""
    return ChangeRecord(
        source="audit",
        user=str(row.get("user") or "unknown"),
        ts=_to_aware_utc(row["sys_created_on"]),
        fieldname=_opt_str(row.get("fieldname")),
        oldvalue=_opt_str(row.get("oldvalue")),
        newvalue=_opt_str(row.get("newvalue")),
        checkpoint=_to_int(row["record_checkpoint"])
        if row.get("record_checkpoint") is not None
        else None,
    )


def journal_row_to_change(row: Mapping[str, Any]) -> ChangeRecord:
    """One ``sys_journal_field`` row → ``ChangeRecord`` (02 §6 ``history``)."""
    return ChangeRecord(
        source="journal",
        user=str(row.get("sys_created_by") or "unknown"),
        ts=_to_aware_utc(row["sys_created_on"]),
        # ``element`` is the journaled field (``work_notes`` / ``comments``);
        # ``name`` is the table name (``incident``) — not the field.
        fieldname=_opt_str(row.get("element")),
        oldvalue=None,
        newvalue=_opt_str(row.get("value")),
        checkpoint=None,
        entry_id=_opt_str(row.get("sys_id")),
    )


def _mapped_field(rec: TicketRecord, field_map: Mapping[str, str], alert_field: str) -> str | None:
    vendor_field = field_map.get(alert_field, DEFAULT_FIELD_MAP[alert_field])
    # A dot-walked vendor field (``cmdb_ci.name``) is stored as ``cmdb_ci_name``.
    return _opt_str(getattr(rec, vendor_field.replace(".", "_"), None))


def record_to_alert(
    tenant_id: str,
    rec: TicketRecord,
    *,
    received_at: datetime,
    field_map: Mapping[str, str] | None = None,
) -> AlertEvent:
    """A new ``TicketRecord`` → ``AlertEvent`` (02 §6 mapping).

    Origin (02 §6): ``em_alert`` or a set ``u_alert_source`` → ``"alert"``;
    otherwise ``"ticket"`` (which requires a ticket number — the domain model
    enforces it). ``severity_hint`` is derived from ``priority``.
    """
    fmap = field_map if field_map is not None else DEFAULT_FIELD_MAP
    origin: Literal["alert", "ticket"] = (
        "alert" if (rec.table == "em_alert" or rec.alert_source is not None) else "ticket"
    )
    subject = _mapped_field(rec, fmap, "subject") or ""
    body = _mapped_field(rec, fmap, "body") or ""
    resource = _mapped_field(rec, fmap, "resource")
    return AlertEvent(
        tenant_id=tenant_id,
        source="servicenow",
        origin=origin,
        external_id=rec.external_id,
        table=rec.table,
        ticket_number=rec.number,
        received_at=received_at,
        subject=subject,
        body=body,
        resource=resource,
        alert_type=rec.alert_source if origin == "alert" else None,
        error_signature=None,
        severity_hint=severity_from_priority(rec.priority),
        cmdb_ci=rec.cmdb_ci,
        raw_ref=f"servicenow:{rec.table}/{rec.external_id}",
    )


__all__ = [
    "DEFAULT_FIELD_MAP",
    "audit_row_to_change",
    "journal_row_to_change",
    "record_to_alert",
    "row_to_record",
    "severity_from_priority",
]
