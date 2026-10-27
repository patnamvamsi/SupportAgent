"""BL-012: recorded developer-instance responses, replayed through the real adapter.

The fixtures come from ``make record-sn`` (``tests/integration/test_record_sn.py``).
A missing fixture skips its test: re-record against a developer instance.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from supportagent.adapters.servicenow.auth import BasicAuth
from supportagent.adapters.servicenow.http import ServiceNowHTTP, build_client
from supportagent.adapters.servicenow.mapping import row_to_record
from supportagent.adapters.servicenow.source import ServiceNowSource
from supportagent.ports.tickets import RecordType

_DATA = Path(__file__).resolve().parents[1] / "data" / "sn"
_SYS_ID = re.compile(r"^[0-9a-f]{32}$")


def _fixture(name: str) -> dict[str, Any]:
    path = _DATA / f"{name}.json"
    if not path.exists():
        pytest.skip(f"{path.name} not recorded yet (make record-sn)")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _source(fixture: dict[str, Any], **by_table: dict[str, Any]) -> ServiceNowSource:
    def handler(request: httpx.Request) -> httpx.Response:
        table = request.url.path.rsplit("/", 1)[-1]
        chosen = by_table.get(table, fixture)
        return httpx.Response(200, json={"result": chosen["result"]})

    http = ServiceNowHTTP(
        http=build_client(
            "https://example.service-now.com", transport=httpx.MockTransport(handler)
        ),
        auth=BasicAuth(user="svc", password="pw-fake"),  # noqa: S106 — replay only
        max_tries=1,
    )
    return ServiceNowSource(http=http)


def test_recorded_poll_maps_every_record() -> None:
    fixture = _fixture("incident_poll")
    batch = _source(fixture).poll(RecordType.INCIDENT, datetime(2000, 1, 1, tzinfo=UTC), limit=5)
    assert len(batch.records) == len(fixture["result"])
    for rec in batch.records:
        assert _SYS_ID.match(rec.external_id)
        for ref in (rec.cmdb_ci, rec.caller_id, rec.assigned_to, rec.assignment_group):
            assert ref is None or _SYS_ID.match(ref), ref
        # BL-045: a record with a CI also carries its name (the alert resource).
        assert (rec.cmdb_ci is None) == (rec.cmdb_ci_name is None)


def test_recorded_reference_objects_unwrap_to_sys_ids() -> None:
    """BL-030: without the exclude flag a real instance sends {"link", "value"}."""
    fixture = _fixture("incident_poll_links")
    linked = [r for r in fixture["result"] if isinstance(r.get("cmdb_ci"), dict)]
    assert linked, "the recording should hold at least one reference object"
    for row in linked:
        rec = row_to_record("incident", row)
        assert rec.cmdb_ci == row["cmdb_ci"]["value"]


def test_recorded_history_merges_audit_and_journal_in_time_order() -> None:
    """02 §6 ``history`` over a real incident's audit trail, work notes and comments."""
    audit, journal = _fixture("incident_audit"), _fixture("incident_journal")
    source = _source(audit, sys_audit=audit, sys_journal_field=journal)
    changes = source.history(RecordType.INCIDENT, "0" * 32, after_mod_count=0)
    assert len(changes) == len(audit["result"]) + len(journal["result"])
    assert [c.ts for c in changes] == sorted(c.ts for c in changes)
    notes = [c for c in changes if c.source == "journal"]
    assert {c.fieldname for c in notes} >= {"work_notes", "comments"}
    assert all(c.entry_id and _SYS_ID.match(c.entry_id) for c in notes)
    assert all(c.user and c.user != "unknown" for c in changes)
