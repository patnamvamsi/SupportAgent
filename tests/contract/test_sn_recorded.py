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
from supportagent.ports.tickets import SnTable

_DATA = Path(__file__).resolve().parents[1] / "data" / "sn"
_SYS_ID = re.compile(r"^[0-9a-f]{32}$")


def _fixture(name: str) -> dict[str, Any]:
    path = _DATA / f"{name}.json"
    if not path.exists():
        pytest.skip(f"{path.name} not recorded yet (make record-sn)")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _source(fixture: dict[str, Any]) -> ServiceNowSource:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": fixture["result"]})

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
    batch = _source(fixture).poll(SnTable.INCIDENT, datetime(2000, 1, 1, tzinfo=UTC), limit=5)
    assert len(batch.records) == len(fixture["result"])
    for rec in batch.records:
        assert _SYS_ID.match(rec.sys_id)
        for ref in (rec.cmdb_ci, rec.caller_id, rec.assigned_to, rec.assignment_group):
            assert ref is None or _SYS_ID.match(ref), ref


def test_recorded_reference_objects_unwrap_to_sys_ids() -> None:
    """BL-030: without the exclude flag a real instance sends {"link", "value"}."""
    fixture = _fixture("incident_poll_links")
    linked = [r for r in fixture["result"] if isinstance(r.get("cmdb_ci"), dict)]
    assert linked, "the recording should hold at least one reference object"
    for row in linked:
        rec = row_to_record("incident", row)
        assert rec.cmdb_ci == row["cmdb_ci"]["value"]
