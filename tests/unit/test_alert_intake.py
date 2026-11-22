"""WP-213: alert intake from Alertmanager and Grafana Alerting (ADR-0022 §2).

Canonical test IDs from 12-wp WP-213: ``test_alertmanager_payload_maps_to_alert_event``,
``test_grafana_alert_payload_maps_to_alert_event``, ``test_receiver_rejects_bad_token_event``,
``test_resolved_alert_never_closes_case_silently``, ``test_alert_service_resolved_from_labels``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.repos.webhooks import WebhookInbox
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app.webhooks import AlertResolutions, WebhookDrainer, WebhookSource
from supportagent.bootstrap import api_context_for
from supportagent.config.schema import Webhook
from supportagent.domain.alertmanager import map_alertmanager
from supportagent.domain.fingerprint import fingerprint
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.severity import Severity
from supportagent.domain.webhook import WebhookRejected
from supportagent.entrypoints.api.app import create_app
from tests.unit.api_support import MARKER, SECRET, TENANT, catalog

_TOKEN = b"alertmanager-bearer-token-0123456789abcdef"


def _alert(status: str = "firing", **labels: str) -> dict[str, Any]:
    return {
        "status": status,
        "labels": {"alertname": "ProgressStopped", "severity": "critical", **labels},
        "annotations": {
            "summary": "payments-api stopped writing",
            "description": "no inserts into ledger for 10 minutes",
        },
        "startsAt": "2026-10-20T09:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus:9090/graph?g0.expr=x",
        "fingerprint": "a1b2c3d4e5f60718",
    }


def _delivery(*alerts: dict[str, Any], status: str = "firing") -> dict[str, Any]:
    return {
        "version": "4",
        "groupKey": '{}:{alertname="ProgressStopped"}',
        "status": status,
        "receiver": "supportagent",
        "groupLabels": {"alertname": "ProgressStopped"},
        "commonLabels": {"env": "prod"},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "alerts": list(alerts),
    }


def test_alertmanager_payload_maps_to_alert_event(clock: Any) -> None:
    doc = _delivery(_alert(service="payments-api"), _alert("resolved", service="ledger"))
    firing, resolved = map_alertmanager(doc, tenant_id=TENANT, source="am", received_at=clock.now())
    assert (firing.source, firing.origin, firing.alert_status) == ("webhook", "alert", "firing")
    assert firing.external_id == "ProgressStopped/a1b2c3d4e5f60718"
    assert firing.subject == "payments-api stopped writing"
    assert firing.body.startswith("no inserts into ledger for 10 minutes\n")
    assert "env=prod" in firing.body, "the delivery's common labels are kept"
    assert "source: http://prometheus:9090/graph" in firing.body
    assert (firing.resource, firing.alert_type) == ("payments-api", "ProgressStopped")
    assert firing.severity_hint is Severity.P1
    assert firing.raw_ref == "webhook:am/ProgressStopped/a1b2c3d4e5f60718"
    assert resolved.alert_status == "resolved"
    assert resolved.resource == "ledger"


def test_grafana_alert_payload_maps_to_alert_event(clock: Any) -> None:
    alert = {
        "status": "firing",
        "labels": {"alertname": "HighLatency", "severity": "warning", "job": "checkout"},
        "annotations": {},
        "startsAt": "2026-10-20T09:00:00Z",
        "valueString": "[ var='B' labels={} value=2.4 ]",
        "dashboardURL": "http://grafana:3000/d/abc",
        "fingerprint": "ffee",
    }
    doc = {**_delivery(alert), "title": "[FIRING:1] HighLatency", "orgId": 1, "state": "alerting"}
    (evt,) = map_alertmanager(doc, tenant_id=TENANT, source="graf", received_at=clock.now())
    assert evt.subject == "HighLatency: checkout", "no summary: alertname and resource"
    assert "value: [ var='B' labels={} value=2.4 ]" in evt.body
    assert "source: http://grafana:3000/d/abc" in evt.body
    assert evt.severity_hint is Severity.P3
    assert evt.resource == "checkout"


def test_alert_without_fingerprint_gets_a_stable_id(clock: Any) -> None:
    bare = {k: v for k, v in _alert(service="payments-api").items() if k != "fingerprint"}
    first = map_alertmanager(
        _delivery(bare), tenant_id=TENANT, source="am", received_at=clock.now()
    )[0]
    again = map_alertmanager(
        _delivery(bare), tenant_id=TENANT, source="am", received_at=clock.now()
    )[0]
    assert first.external_id == again.external_id
    assert first.external_id.startswith("ProgressStopped/")


@pytest.mark.parametrize(
    "doc",
    [
        [],
        {"alerts": "nope"},
        {"alerts": [{"status": "pending", "labels": {}}]},
        {"alerts": [{"status": "firing", "labels": {"a": 1}}]},
        {"alerts": [{"status": "firing"}] * 1001},
    ],
)
def test_malformed_alert_batch_rejected(doc: object, clock: Any) -> None:
    with pytest.raises(WebhookRejected) as err:
        map_alertmanager(doc, tenant_id=TENANT, source="am", received_at=clock.now())
    assert err.value.reason == "bad_payload"


def test_alert_service_resolved_from_labels(clock: Any) -> None:
    labels = {
        "container": "payments-api-1",
        "container_label_com_docker_compose_service": "payments-api",
        "job": "cadvisor",
    }
    (evt,) = map_alertmanager(
        _delivery(_alert(**labels)), tenant_id=TENANT, source="am", received_at=clock.now()
    )
    assert evt.resource == "payments-api", "the compose service outranks container and job"
    assert catalog("payments").resolve_service(resource=evt.resource) == "payments"
    (custom,) = map_alertmanager(
        _delivery(_alert(**labels)),
        tenant_id=TENANT,
        source="am",
        received_at=clock.now(),
        service_labels=("job",),
    )
    assert custom.resource == "cadvisor", "a source can name its own label order"
    (none,) = map_alertmanager(
        _delivery(_alert()), tenant_id=TENANT, source="am", received_at=clock.now()
    )
    assert none.resource is None, "no service label: the catalog safe-defaults"


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
        webhook_sources={
            "am": WebhookSource(secret=_TOKEN, mapping={}, kind="alertmanager"),
        },
    )
    return TestClient(create_app(ctx))


def _post(client: TestClient, doc: object, token: bytes | None = _TOKEN) -> Any:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token.decode()}"
    return client.post("/api/v1/webhooks/am", content=json.dumps(doc), headers=headers)


def _count(db: Any, sql: str) -> int:
    with db.connect() as conn:
        return int(conn.execute(text(sql)).scalar_one())


def test_receiver_accepts_a_batch(client: TestClient, db: Any) -> None:
    resp = _post(client, _delivery(_alert(service="payments-api"), _alert(service="ledger")))
    assert resp.status_code == 202, resp.text
    assert len(resp.json()["accepted"]) == 2, "one queued alert per alert in the batch"
    assert _count(db, "SELECT COUNT(*) FROM webhook_inbox") == 2
    again = _post(client, _delivery(_alert(service="payments-api")))
    assert again.status_code == 202, "a repeat notification is accepted; dedup absorbs it"


def test_receiver_rejects_bad_token_event(client: TestClient, db: Any) -> None:
    doc = _delivery(_alert(service="payments-api"))
    assert _post(client, doc, token=b"wrong-token-wrong-token-wrong-token!!").status_code == 401
    assert _post(client, doc, token=None).status_code == 401
    assert _post(client, {"alerts": "nope"}).status_code == 422
    assert _count(db, "SELECT COUNT(*) FROM webhook_inbox") == 0
    assert _count(db, "SELECT COUNT(*) FROM events WHERE kind = 'webhook_rejected'") == 3


def _open_case(db: Any, evt: AlertEvent, status: str = "open") -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, trace_id, "
                "duplicate_count, opened_ts, last_alert_ts) VALUES ('case_1', :t, :fp, "
                "'alert', :status, 'tr_case_1', 0, 1, 1)"
            ),
            {"t": TENANT, "fp": fingerprint(evt), "status": status},
        )


def test_resolved_alert_never_closes_case_silently(  # noqa: PLR0913, PLR0917 — fixtures
    client: TestClient, db: Any, uow: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    firing, resolved = map_alertmanager(
        _delivery(_alert(service="payments-api"), _alert("resolved", service="payments-api")),
        tenant_id=TENANT,
        source="am",
        received_at=clock.now(),
    )
    assert fingerprint(firing) == fingerprint(resolved), "both name the same incident"
    _open_case(db, firing)
    assert _post(client, _delivery(_alert("resolved", service="payments-api"))).status_code == 202
    triaged: list[AlertEvent] = []

    class _Runner:
        def run(self, evt: AlertEvent) -> None:
            triaged.append(evt)

    aead = Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock)
    drainer = WebhookDrainer(
        uow=uow,
        inbox=WebhookInbox(uow, aead),
        runner=_Runner(),
        clock=clock,
        tenant_id=TENANT,
        resolutions=AlertResolutions(
            uow=uow,
            clock=clock,
            ids=ids,
            events=SqlEventSink(uow=uow, cipher=aead),
            tenant_id=TENANT,
        ),
    )
    assert drainer.run_once() == 1
    assert triaged == [], "a resolved alert is never triaged as a new incident"
    assert _count(db, "SELECT COUNT(*) FROM cases WHERE status = 'open'") == 1, "status kept"
    assert (
        _count(
            db,
            "SELECT COUNT(*) FROM events WHERE kind = 'sync' AND trace_id = 'tr_case_1'",
        )
        == 1
    ), "the case's timeline records that the source resolved the alert"
    assert _post(client, _delivery(_alert(service="payments-api"))).status_code == 202
    assert drainer.run_once() == 1
    assert [e.alert_status for e in triaged] == ["firing"]


def test_resolved_alert_without_open_case_notes_nothing(
    db: Any, uow: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    (resolved,) = map_alertmanager(
        _delivery(_alert("resolved", service="payments-api")),
        tenant_id=TENANT,
        source="am",
        received_at=clock.now(),
    )
    _open_case(db, resolved, status="resolved")
    aead = Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock)
    notes = AlertResolutions(
        uow=uow, clock=clock, ids=ids, events=SqlEventSink(uow=uow, cipher=aead), tenant_id=TENANT
    )
    assert notes.record(resolved) is False
    assert _count(db, "SELECT COUNT(*) FROM events") == 0


def test_alert_kind_config_uses_its_preset() -> None:
    hook = Webhook(source="am", kind="alertmanager", secret=_TOKEN.decode())  # type: ignore[arg-type]
    assert hook.mapping == {} and hook.service_labels[0] == "service"
    with pytest.raises(ValueError, match="preset"):
        Webhook(
            source="am",
            kind="grafana",
            secret=_TOKEN.decode(),  # type: ignore[arg-type]
            mapping={"external_id": "$.id", "subject": "$.t"},
        )
    with pytest.raises(ValueError, match="mapping needs"):
        Webhook(source="nagios", secret=_TOKEN.decode())  # type: ignore[arg-type]


def _alerts_only(**update: object) -> dict[str, Any]:
    from tests.unit.test_triage_service import _shadow_config  # noqa: PLC0415

    hook = Webhook(source="am", kind="alertmanager", secret=_TOKEN.decode())  # type: ignore[arg-type]
    return {**_shadow_config().model_dump(), "servicenow": None, "webhooks": [hook], **update}


def test_an_alerts_only_deployment_needs_no_ticket_connector() -> None:
    """ADR-0022: a system with no ticket source is triaged from its alerts, in shadow only."""
    from supportagent.config.schema import AppConfig  # noqa: PLC0415

    assert AppConfig.model_validate(_alerts_only()).servicenow is None
    with pytest.raises(ValueError, match="configure a ticket connector"):
        AppConfig.model_validate(_alerts_only(mode="assisted"))
    with pytest.raises(ValueError, match="nothing to triage"):
        AppConfig.model_validate(_alerts_only(webhooks=[]))


def test_the_doctor_of_an_alerts_only_deployment(tmp_path: Any) -> None:
    from sqlalchemy import create_engine  # noqa: PLC0415

    from supportagent.app.doctor.checks import default_checks  # noqa: PLC0415
    from supportagent.app.doctor.connectors import connector_doctor  # noqa: PLC0415
    from supportagent.bootstrap import build_doctor_context, ticket_connector  # noqa: PLC0415
    from supportagent.config.schema import AppConfig  # noqa: PLC0415
    from supportagent.domain.errors import ConfigError  # noqa: PLC0415

    config = AppConfig.model_validate(_alerts_only())
    ctx = build_doctor_context(config, engine=create_engine(f"sqlite:///{tmp_path}/d.db"))
    assert ctx.connector.name == "none"
    checks = {c.name: c for c in default_checks(ctx.connector.name)}
    intake = checks["none.alert_intake"].run(ctx)
    assert intake.status == "pass" and "(am)" in intake.detail
    assert checks["connector"].run(ctx).status == "warn", "shadow only, said out loud"
    assert connector_doctor("none").discover_automation(ctx) == [], "no ticket assignees"
    with pytest.raises(ConfigError, match="no ticket connector"):
        ctx.records.read("incident", query="", fields=["sys_id"], limit=1)
    with pytest.raises(ConfigError, match="no ticket connector"):
        ticket_connector(config)
