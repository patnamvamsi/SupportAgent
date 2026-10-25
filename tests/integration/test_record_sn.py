"""BL-012: record ServiceNow Table API fixtures from a developer instance (04 §3).

``make record-sn SN_HOST=<host>`` runs this module (``-m "integration and sn"``)
with ``SN_USER`` / ``SN_PASSWORD`` set (basic auth). It writes the instance's
raw responses to ``tests/data/sn/*.json``; ``tests/contract/test_sn_recorded.py``
replays them through the real adapter, offline. Re-record each release.

Each request is the one the adapter sends (``ServiceNowSource`` fields and
flags), plus one without ``sysparm_exclude_reference_link`` so the
reference-object shape stays covered (BL-030). Instance URLs in the bodies are
rewritten to ``example.service-now.com``. The developer instance holds only
ServiceNow demo data; nothing is written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from supportagent.adapters.servicenow.source import AUDIT_FIELDS, JOURNAL_FIELDS, POLL_FIELDS

pytestmark = [pytest.mark.integration, pytest.mark.sn]

_OUT = Path(__file__).resolve().parents[1] / "data" / "sn"
_HOST = os.environ.get("SN_HOST", "")
_SINCE = "2000-01-01 00:00:00"
_FORBIDDEN = 403


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    if not (_HOST and os.environ.get("SN_USER") and os.environ.get("SN_PASSWORD")):
        pytest.skip("set SN_HOST, SN_USER and SN_PASSWORD to record (make record-sn)")
    return httpx.Client(
        base_url=f"https://{_HOST}",
        auth=(os.environ["SN_USER"], os.environ["SN_PASSWORD"]),
        headers={"Accept": "application/json"},
        timeout=60,
    )


def _record(client: httpx.Client, name: str, table: str, params: dict[str, Any]) -> list[Any]:
    resp = client.get(f"/api/now/table/{table}", params=params)
    if resp.status_code == _FORBIDDEN:
        pytest.skip(f"{table}: 403 for the integration user (doctor servicenow.history_access)")
    resp.raise_for_status()
    body = resp.text.replace(f"https://{_HOST}", "https://example.service-now.com")
    result = json.loads(body)["result"]
    fixture = {"table": table, "params": params, "status": resp.status_code, "result": result}
    (_OUT / f"{name}.json").write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n")
    return list(result)


def _poll_params(*, exclude_links: bool) -> dict[str, Any]:
    params: dict[str, Any] = {
        "sysparm_query": f"sys_updated_on>={_SINCE}^ORDERBYsys_updated_on^ORDERBYsys_id",
        "sysparm_limit": 5,
        "sysparm_offset": 0,
        "sysparm_fields": POLL_FIELDS,
        "sysparm_display_value": "false",
    }
    if exclude_links:
        params["sysparm_exclude_reference_link"] = "true"
    return params


def test_record_incident_poll(client: httpx.Client) -> None:
    rows = _record(client, "incident_poll", "incident", _poll_params(exclude_links=True))
    assert rows, "the developer instance has no incidents"
    _record(client, "incident_poll_links", "incident", _poll_params(exclude_links=False))


def test_record_incident_history(client: httpx.Client) -> None:
    poll = json.loads((_OUT / "incident_poll.json").read_text())["result"]
    sys_id = poll[0]["sys_id"]
    flags = {"sysparm_display_value": "false", "sysparm_exclude_reference_link": "true"}
    _record(
        client,
        "incident_audit",
        "sys_audit",
        {
            "sysparm_query": f"tablename=incident^documentkey={sys_id}",
            "sysparm_fields": AUDIT_FIELDS,
        }
        | flags,
    )
    _record(
        client,
        "incident_journal",
        "sys_journal_field",
        {"sysparm_query": f"name=incident^element_id={sys_id}"}
        | {"sysparm_fields": JOURNAL_FIELDS}
        | flags,
    )
