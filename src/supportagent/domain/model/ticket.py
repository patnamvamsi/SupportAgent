"""Inbound ticket records (01 §4, 02 §6): the normalized poll/history shape.

``TicketRecord`` is the current state of one changed ServiceNow record, as
returned by ``TicketSource.poll`` (02 §6). ``ChangeRecord`` is one change —
an ``sys_audit`` field edit or a ``sys_journal_field`` entry — as returned by
``TicketSource.history`` (02 §6). Both are domain types: frozen, extra-forbid,
strict, aware-UTC (01 §4). The vendor's raw JSON is mapped into these by the
adapter (``adapters.servicenow.mapping``); no vendor type ever crosses the
port (01 §3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from supportagent.domain.model.base import DomainModel


class TicketRecord(DomainModel):
    """Current state of one changed record (02 §6 ``poll`` result row).

    ``table`` is the ServiceNow table name (``"incident"`` / ``"em_alert"``) as
    a plain string — the domain does not import the ``SnTable`` port enum
    (domain never depends on ports; 01 §1). ``priority`` is the vendor's raw
    value (``"1"``..``"4"``); ``severity_hint`` is derived at mapping time.
    """

    table: str
    sys_id: str
    number: str | None = None
    short_description: str | None = None
    description: str | None = None
    priority: str | None = None
    state: str | None = None
    assigned_to: str | None = None
    assignment_group: str | None = None
    cmdb_ci: str | None = None
    #: BL-045: the CI's name (``cmdb_ci.name``), what a catalog's ``resources`` list.
    cmdb_ci_name: str | None = None
    caller_id: str | None = None
    u_alert_source: str | None = None
    sys_updated_on: datetime
    sys_updated_by: str
    sys_mod_count: int


class ChangeRecord(DomainModel):
    """One change to a record (02 §6 ``history`` result row).

    ``source`` distinguishes the two ServiceNow change feeds: ``"audit"``
    (``sys_audit`` field edits) and ``"journal"`` (``sys_journal_field``
    work-note / comment entries). ``user`` is the actor — the sync service's
    self-echo filter keys on it (ARCH §8.1). ``checkpoint`` is the
    ``sys_audit.record_checkpoint`` (== the record's ``sys_mod_count`` at the
    edit); ``None`` for journal entries, which are ordered by ``ts`` instead.
    """

    source: Literal["audit", "journal"]
    user: str
    ts: datetime
    fieldname: str | None = None
    oldvalue: str | None = None
    newvalue: str | None = None
    checkpoint: int | None = None
    #: BL-013: the ``sys_journal_field`` row's ``sys_id``, so a journal entry
    #: re-read inside the poll overlap window is delivered once.
    entry_id: str | None = None


__all__ = ["ChangeRecord", "TicketRecord"]
