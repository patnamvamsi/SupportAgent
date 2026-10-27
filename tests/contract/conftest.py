"""Shared fixtures for the WP-008 contract suite (10-wp L124-127).

The contract is pinned against **both** implementations of the
``TicketSource`` / ``TicketSink`` ports:

* ``fake``  — the in-memory ``FakeServiceNow`` (``adapters.fakes.servicenow``);
* ``http``  — the real ServiceNow adapter (``adapters.servicenow``) driven over
  its own HTTP surface by the ``FakeServiceNowHTTP`` ASGI app +
  ``SyncASGITransport`` (so the retry policy, query parsing, and JSON mapping
  are all exercised — not just the in-memory logic).

``sn_env`` is a small uniform view over whichever implementation is active:
``source`` / ``sink`` (the port), the seeders, a ``write_count`` callable (the
sink's successful writes, or the ASGI fake's successful PATCHes), and a
``read_field`` reader. The two HTTP-retry tests in
``ticket_source_contract`` additionally use the ``sn_real`` stack directly so
they can queue raw 429/5xx responses and assert on the recorded sleep seam.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from supportagent.adapters.fakes.servicenow import (
    FakeServiceNow,
    FakeServiceNowHTTP,
    SyncASGITransport,
)
from supportagent.adapters.servicenow import BasicAuth, ServiceNow, ServiceNowHTTP


def _real_stack() -> Any:
    """The real adapter wired over the ASGI fake, with a recording sleep seam."""
    fake_http = FakeServiceNowHTTP()
    sleeps: list[float] = []
    client = httpx.Client(transport=SyncASGITransport(fake_http.app), base_url="http://sn")
    sn_http = ServiceNowHTTP(
        http=client,
        auth=BasicAuth(user="svc_supportagent", password="secret"),  # noqa: S106
        sleep=sleeps.append,  # record, never wait
        jitter=None,
    )
    facade = ServiceNow(http=sn_http)
    return {"facade": facade, "sn_http": sn_http, "fake_http": fake_http, "sleeps": sleeps}


@pytest.fixture
def sn_real() -> Any:
    """The real adapter stack (facade + http + fake-http + recorded sleeps)."""
    return _real_stack()


@pytest.fixture(params=["inmem", "real"], ids=["fake", "http"])
def sn_env(request: pytest.FixtureRequest) -> Any:
    """A uniform view over one ``TicketSource`` + ``TicketSink`` implementation."""
    if request.param == "inmem":
        fake = FakeServiceNow()
        return {
            "source": fake,
            "sink": fake,
            "seed": fake.seed_record,
            "seed_audit": fake.seed_audit,
            "seed_journal": fake.seed_journal,
            "write_count": lambda: fake.write_count,  # live read of the property
            "read_field": lambda table, external_id, field: fake.fields(table, external_id).get(
                field
            ),
        }
    stack = _real_stack()
    fake_http = stack["fake_http"]

    def _read_field(table: Any, external_id: str, field: str) -> Any:
        return fake_http._rows.get(table.value, {}).get(external_id, {}).get(field)

    return {
        "source": stack["facade"],
        "sink": stack["facade"],
        "seed": fake_http.seed_record,
        "seed_audit": fake_http.seed_audit,
        "seed_journal": fake_http.seed_journal,
        "write_count": lambda: fake_http.patch_count,  # live read of the property
        "read_field": _read_field,
    }
