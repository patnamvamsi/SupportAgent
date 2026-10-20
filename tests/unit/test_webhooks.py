"""WP-113: the generic webhook adapter (ARCH §7, 02 §2).

Canonical test IDs from 11-wp §WP-113: ``test_webhook_valid_signature_accepted``,
``test_webhook_bad_signature_rejected_event``, ``test_webhook_replay_nonce_rejected``,
``test_webhook_stale_timestamp_rejected``, ``test_webhook_maps_to_alert_event``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.webhooks import WebhookInbox
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app.webhooks import WebhookDrainer, WebhookSource
from supportagent.bootstrap import api_context_for
from supportagent.config.schema import Webhook
from supportagent.domain.errors import DomainValidationError
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.severity import Severity
from supportagent.domain.webhook import extract, signature, validate_mapping
from supportagent.entrypoints.api.app import create_app
from tests.unit.api_support import MARKER, SECRET, TENANT, catalog

_SECRET = b"nagios-webhook-secret-0123456789abcdef"
_MAPPING = {
    "external_id": "$.id",
    "subject": "$.title",
    "body": "$.detail.message",
    "resource": "$.host",
    "alert_type": "$.check",
    "severity_hint": "$.severity",
    "error_signature": "$.tags[0]",
}
_DOC = {
    "id": "evt-42",
    "title": "payments-api 5xx",
    "detail": {"message": "error rate 12% over 5 min"},
    "host": "payments-api",
    "check": "http_5xx",
    "severity": "high",
    "tags": ["5xx:/v1/pay", "prod"],
}


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes) -> TestClient:
    ctx = api_context_for(
        db,
        tenant_id=TENANT,
        catalog=catalog("payments"),
        master_key=master_key,
        clock=clock,
        ids=ids,
        identity=FakeOIDC(),
        session_secret=SECRET,
        secure_cookies=False,
        config_marker=MARKER,
        webhook_sources={"nagios": WebhookSource(secret=_SECRET, mapping=_MAPPING)},
    )
    return TestClient(create_app(ctx))


def _post(  # noqa: PLR0913 — one knob per signed header
    client: TestClient,
    clock: Any,
    body: bytes,
    *,
    nonce: str = "n-1",
    ts: int | None = None,
    secret: bytes = _SECRET,
    source: str = "nagios",
) -> Any:
    stamp = str(ts if ts is not None else int(clock.now().timestamp()))
    return client.post(
        f"/api/v1/webhooks/{source}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-SA-Timestamp": stamp,
            "X-SA-Nonce": nonce,
            "X-SA-Signature": signature(secret, stamp, body),
        },
    )


def _rejections(db: Any) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM events WHERE kind = 'webhook_rejected'")
            ).scalar_one()
        )


def _inbox(db: Any) -> int:
    with db.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM webhook_inbox")).scalar_one())


def test_webhook_valid_signature_accepted(client: TestClient, clock: Any, db: Any) -> None:
    resp = _post(client, clock, json.dumps(_DOC).encode())
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"]
    assert _inbox(db) == 1 and _rejections(db) == 0
    with db.connect() as conn:
        stored = conn.execute(text("SELECT event_enc FROM webhook_inbox")).scalar_one()
    assert b"payments-api" not in bytes(stored), "the inbox is encrypted at rest"


def test_webhook_bad_signature_rejected_event(client: TestClient, clock: Any, db: Any) -> None:
    body = json.dumps(_DOC).encode()
    forged = _post(client, clock, body, secret=b"wrong-secret-wrong-secret-wrong-secret")
    assert forged.status_code == 401
    unknown = _post(client, clock, body, source="zabbix")
    assert unknown.status_code == 401, "an unknown source looks the same as a bad signature"
    assert _inbox(db) == 0
    assert _rejections(db) == 2
    with db.connect() as conn:
        actors = [r[0] for r in conn.execute(text("SELECT actor FROM events ORDER BY ts_ms"))]
    assert actors == ["webhook:nagios", "webhook:zabbix"]


def test_webhook_replay_nonce_rejected(client: TestClient, clock: Any, db: Any) -> None:
    body = json.dumps(_DOC).encode()
    assert _post(client, clock, body, nonce="once").status_code == 202
    assert _post(client, clock, body, nonce="once").status_code == 401
    assert _post(client, clock, body, nonce="").status_code == 401
    assert _inbox(db) == 1 and _rejections(db) == 2


def test_webhook_stale_timestamp_rejected(client: TestClient, clock: Any, db: Any) -> None:
    body = json.dumps(_DOC).encode()
    now = int(clock.now().timestamp())
    assert _post(client, clock, body, ts=now - 301, nonce="a").status_code == 401
    assert _post(client, clock, body, ts=now + 301, nonce="b").status_code == 401
    assert _post(client, clock, body, ts=now - 299, nonce="c").status_code == 202
    assert _rejections(db) == 2


def test_webhook_maps_to_alert_event(
    client: TestClient, clock: Any, db: Any, uow: Any, master_key: bytes
) -> None:
    assert _post(client, clock, json.dumps(_DOC).encode()).status_code == 202
    seen: list[AlertEvent] = []

    class _Runner:
        def run(self, evt: AlertEvent) -> None:
            seen.append(evt)

    inbox = WebhookInbox(uow, Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock))
    drainer = WebhookDrainer(uow=uow, inbox=inbox, runner=_Runner(), clock=clock, tenant_id=TENANT)
    assert drainer.run_once() == 1
    assert drainer.run_once() == 0, "a processed delivery isn't triaged twice"
    (evt,) = seen
    assert (evt.source, evt.origin, evt.external_id) == ("webhook", "alert", "evt-42")
    assert evt.subject == "payments-api 5xx"
    assert evt.body == "error rate 12% over 5 min"
    assert (evt.resource, evt.alert_type, evt.error_signature) == (
        "payments-api",
        "http_5xx",
        "5xx:/v1/pay",
    )
    assert evt.severity_hint is Severity.P2
    assert evt.raw_ref == "webhook:nagios/evt-42"


def test_webhook_unmappable_body_is_422(client: TestClient, clock: Any, db: Any) -> None:
    assert _post(client, clock, b"not json", nonce="x").status_code == 422
    no_subject = {k: v for k, v in _DOC.items() if k != "title"}
    assert _post(client, clock, json.dumps(no_subject).encode(), nonce="y").status_code == 422
    nested = {**_DOC, "title": {"oops": 1}}
    assert _post(client, clock, json.dumps(nested).encode(), nonce="z").status_code == 422
    assert _inbox(db) == 0 and _rejections(db) == 3


def test_paths_and_mapping_validation() -> None:
    assert extract(_DOC, "$.detail.message") == "error rate 12% over 5 min"
    assert extract(_DOC, "$.tags[1]") == "prod"
    assert extract(_DOC, "$.tags[9]") is None
    assert extract(_DOC, "$.nope.deeper") is None
    with pytest.raises(DomainValidationError):
        validate_mapping({"external_id": "$.id"})  # subject is required
    with pytest.raises(DomainValidationError):
        validate_mapping({"external_id": "$.id", "subject": "$..title"})  # no recursion
    with pytest.raises(DomainValidationError):
        validate_mapping({"external_id": "$.id", "subject": "$.t", "tenant_id": "$.x"})


def test_webhook_config_needs_a_long_secret() -> None:
    ok = Webhook(source="nagios", secret=_SECRET.decode(), mapping=_MAPPING)  # type: ignore[arg-type]
    assert ok.source == "nagios"
    with pytest.raises(ValueError, match="at least 32"):
        Webhook(source="nagios", secret="short", mapping=_MAPPING)  # type: ignore[arg-type]  # noqa: S106


def test_csrf_exemption_is_only_for_webhooks(client: TestClient) -> None:
    # Any other POST without the CSRF header is still refused.
    assert client.post("/api/v1/setup/approve").status_code == 403
