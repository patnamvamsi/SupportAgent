"""The ServiceNow HTTP client factory applies the 02 §6 timeouts.

``ServiceNowHTTP`` takes an injected ``httpx.Client`` (tests use the ASGI
fake's sync transport); ``build_client`` is the production factory that must
carry the spec'd 10 s connect / 30 s read timeouts.
"""

from __future__ import annotations

import httpx

from supportagent.adapters.servicenow.http import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_READ_TIMEOUT_S,
    build_client,
)


def test_build_client_applies_spec_timeouts() -> None:
    client = build_client("http://sn")
    try:
        assert client.timeout.connect == DEFAULT_CONNECT_TIMEOUT_S
        assert client.timeout.connect == 10.0
        assert client.timeout.read == DEFAULT_READ_TIMEOUT_S
        assert client.timeout.read == 30.0
    finally:
        client.close()


def test_build_client_accepts_injected_transport() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"result": []}))
    client = build_client("http://sn", transport=transport)
    try:
        resp = client.get("/api/now/table/incident")
        assert resp.status_code == 200
        assert resp.json() == {"result": []}
    finally:
        client.close()
