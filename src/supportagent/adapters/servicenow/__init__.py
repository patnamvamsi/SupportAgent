"""supportagent.adapters.servicenow (01 §1 layout; 02 §5-§6).

The real ServiceNow adapter: ``ServiceNow`` (a ``TicketSource`` + ``TicketSink``
over one instance) backed by ``ServiceNowHTTP`` (the retrying Table-API client)
and ``mapping`` (JSON → domain). ``BasicAuth`` / ``OAuthClientCredentials``
provide the ``Authorization`` header (02 §1).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from supportagent.adapters.servicenow.auth import BasicAuth, OAuthClientCredentials, ServiceNowAuth
from supportagent.adapters.servicenow.http import ServiceNowHTTP, build_client
from supportagent.adapters.servicenow.mapping import DEFAULT_FIELD_MAP, record_to_alert
from supportagent.adapters.servicenow.sink import ServiceNowSink
from supportagent.adapters.servicenow.source import ServiceNowSource
from supportagent.domain.errors import ServiceNowError
from supportagent.domain.model.external import ExternalRef
from supportagent.domain.model.ticket import ChangeRecord
from supportagent.ports.tickets import JournalField, PollBatch, SnTable


class ServiceNow:
    """The real ServiceNow adapter (02 §5-§6): one instance, read + write.

    Satisfies both ``TicketSource`` (``poll`` / ``history``) and ``TicketSink``
    (``append_journal`` / ``update_fields`` / ``find_marker``).
    """

    def __init__(
        self,
        *,
        http: ServiceNowHTTP,
        field_map: Mapping[str, str] | None = None,
    ) -> None:
        self._http = http
        self._field_map = field_map if field_map is not None else DEFAULT_FIELD_MAP
        self._source = ServiceNowSource(http=http)
        self._sink = ServiceNowSink(http=http)

    # -- TicketSource (02 §6) --------------------------------------------

    def poll(self, table: SnTable, since: datetime, limit: int) -> PollBatch:
        return self._source.poll(table, since, limit)

    def history(self, table: SnTable, sys_id: str, after_mod_count: int) -> Sequence[ChangeRecord]:
        return self._source.history(table, sys_id, after_mod_count)

    # -- TicketSink (01 §7.5) --------------------------------------------

    def append_journal(
        self,
        table: SnTable,
        sys_id: str,
        field: JournalField,
        text: str,
    ) -> ExternalRef:
        return self._sink.append_journal(table, sys_id, field, text)

    def update_fields(
        self,
        table: SnTable,
        sys_id: str,
        fields: Mapping[str, str],
    ) -> ExternalRef:
        return self._sink.update_fields(table, sys_id, fields)

    def find_marker(
        self,
        table: SnTable,
        sys_id: str,
        marker: str,
    ) -> ExternalRef | None:
        return self._sink.find_marker(table, sys_id, marker)


__all__ = [
    "DEFAULT_FIELD_MAP",
    "BasicAuth",
    "JournalField",
    "OAuthClientCredentials",
    "PollBatch",
    "ServiceNow",
    "ServiceNowAuth",
    "ServiceNowError",
    "ServiceNowHTTP",
    "ServiceNowSink",
    "ServiceNowSource",
    "SnTable",
    "build_client",
    "record_to_alert",
]
