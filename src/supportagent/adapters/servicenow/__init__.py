"""supportagent.adapters.servicenow (01 §1 layout; 02 §5-§6; WP-M07).

The real ServiceNow connector: ``ServiceNow`` (a ``Connector``, meaning a
``TicketSource`` + ``TicketSink`` over one instance plus its manifest and
semantics) backed by ``ServiceNowHTTP`` (the retrying Table-API client) and
``mapping`` (JSON → domain). ``BasicAuth`` / ``OAuthClientCredentials``
provide the ``Authorization`` header (02 §1).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from supportagent.adapters.servicenow.auth import BasicAuth, OAuthClientCredentials, ServiceNowAuth
from supportagent.adapters.servicenow.errors import ServiceNowError
from supportagent.adapters.servicenow.http import ServiceNowHTTP, build_client
from supportagent.adapters.servicenow.mapping import DEFAULT_FIELD_MAP, record_to_alert
from supportagent.adapters.servicenow.semantics import SERVICENOW_MANIFEST, ServiceNowSemantics
from supportagent.adapters.servicenow.sink import ServiceNowSink
from supportagent.adapters.servicenow.source import ServiceNowSource
from supportagent.domain.connector import ConnectorManifest
from supportagent.domain.model.external import ExternalRef
from supportagent.domain.model.ticket import ChangeRecord, TicketRecord
from supportagent.ports.tickets import NoteVisibility, PollBatch, RecordType


class ServiceNow:
    """The real ServiceNow adapter (02 §5-§6): one instance, read + write.

    Satisfies ``Connector``: ``TicketSource`` (``poll`` / ``history``),
    ``TicketSink`` (``append_journal`` / ``update_fields`` / ``find_marker``),
    the full capability manifest, and the instance's semantics.
    """

    def __init__(
        self,
        *,
        http: ServiceNowHTTP,
        field_map: Mapping[str, str] | None = None,
        semantics: ServiceNowSemantics | None = None,
    ) -> None:
        self._http = http
        self._semantics = semantics or ServiceNowSemantics()
        self._field_map = field_map if field_map is not None else DEFAULT_FIELD_MAP
        self._source = ServiceNowSource(http=http)
        self._sink = ServiceNowSink(http=http)

    # -- Connector (WP-M07) ----------------------------------------------

    @property
    def manifest(self) -> ConnectorManifest:
        return SERVICENOW_MANIFEST

    @property
    def semantics(self) -> ServiceNowSemantics:
        return self._semantics

    # -- TicketSource (02 §6) --------------------------------------------

    def poll(self, table: RecordType, since: datetime, limit: int, offset: int = 0) -> PollBatch:
        return self._source.poll(table, since, limit, offset)

    def record(self, table: RecordType, external_id: str) -> TicketRecord | None:
        return self._source.record(table, external_id)

    def history(
        self,
        table: RecordType,
        external_id: str,
        after_mod_count: int,
        since: datetime | None = None,
    ) -> Sequence[ChangeRecord]:
        return self._source.history(table, external_id, after_mod_count, since)

    # -- TicketSink (01 §7.5) --------------------------------------------

    def append_journal(
        self,
        table: RecordType,
        external_id: str,
        visibility: NoteVisibility,
        text: str,
    ) -> ExternalRef:
        return self._sink.append_journal(table, external_id, visibility, text)

    def update_fields(
        self,
        table: RecordType,
        external_id: str,
        fields: Mapping[str, str],
    ) -> ExternalRef:
        return self._sink.update_fields(table, external_id, fields)

    def find_marker(
        self,
        table: RecordType,
        external_id: str,
        marker: str,
    ) -> ExternalRef | None:
        return self._sink.find_marker(table, external_id, marker)


__all__ = [
    "DEFAULT_FIELD_MAP",
    "SERVICENOW_MANIFEST",
    "BasicAuth",
    "NoteVisibility",
    "OAuthClientCredentials",
    "PollBatch",
    "RecordType",
    "ServiceNow",
    "ServiceNowAuth",
    "ServiceNowError",
    "ServiceNowHTTP",
    "ServiceNowSemantics",
    "ServiceNowSink",
    "ServiceNowSource",
    "build_client",
    "record_to_alert",
]
