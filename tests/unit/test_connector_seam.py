"""WP-M07: the connector seam (14-wp; ADR-0011 §3).

The ticket ports are a connector contract (``ports.tickets.Connector``,
``domain.connector``); ServiceNow is refactored onto it with no behaviour
change; a connector missing a capability degrades safely; and each connector
registers its own doctor checks.
"""

from __future__ import annotations

import ast
import configparser
import dataclasses
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from supportagent.adapters.db.repos.sync import SyncRepo
from supportagent.adapters.fakes.servicenow import (
    FakeServiceNow,
    FakeServiceNowHTTP,
    SyncASGITransport,
)
from supportagent.adapters.servicenow import BasicAuth, ServiceNow, ServiceNowHTTP
from supportagent.adapters.servicenow.mapping import record_to_alert
from supportagent.adapters.servicenow.semantics import (
    SERVICENOW_MANIFEST,
    ServiceNowSemantics,
    severity_from_priority,
)
from supportagent.app.doctor import default_checks
from supportagent.app.doctor.checks.connector import ConnectorCheck
from supportagent.app.doctor.connectors import CONNECTOR_DOCTORS, connector_doctor
from supportagent.app.sync import SyncService
from supportagent.bootstrap import require_connector_supports_mode
from supportagent.domain.connector import (
    ActorKind,
    Capability,
    ConnectorManifest,
    classify_actor,
    limits_for,
)
from supportagent.domain.errors import ConfigError
from supportagent.domain.severity import Severity
from supportagent.ports.tickets import Connector, NoteVisibility, RecordType
from tests.builders import T0
from tests.unit.test_doctor import _ctx
from tests.unit.test_sync_service import _RecordingHandler, _seed
from tests.unit.test_triage_service import _shadow_config

_REPO = Path(__file__).resolve().parents[2]
_CORE = ("domain", "ports", "app")
#: Connector-specific doctor checks live here by design (WP-M07).
_CONNECTOR_PLUGINS = _REPO / "src/supportagent/app/doctor/connectors"
#: Vendor names that must never appear as a code identifier in the core.
_VENDOR_NAMES = frozenset(
    {
        "SnTable",
        "JournalField",
        "ServiceNowError",
        "ServiceNowSemantics",
        "SERVICENOW_MANIFEST",
        "ServiceNowProbe",
        "sys_id",
        "sys_mod_count",
        "sys_updated_on",
        "sys_updated_by",
        "sys_created_by",
        "u_alert_source",
        "map_servicenow_classification",
    }
)


def _identifiers(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute):
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.arg | ast.keyword):
            found.append((node.lineno, node.arg or ""))
        elif isinstance(node, ast.FunctionDef | ast.ClassDef):
            found.append((node.lineno, node.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


def test_core_has_no_servicenow_types() -> None:
    """The import-linter contract exists, and no core code names a vendor type or field."""
    contracts = configparser.ConfigParser()
    contracts.read(_REPO / ".importlinter")
    section = contracts["importlinter:contract:core-no-servicenow"]
    forbidden = section["forbidden_modules"].split()
    assert "supportagent.adapters.servicenow" in forbidden
    assert {"supportagent.domain", "supportagent.ports", "supportagent.app"} <= set(
        section["source_modules"].split()
    )

    leaks: list[str] = []
    for layer in _CORE:
        for path in sorted((_REPO / "src/supportagent" / layer).rglob("*.py")):
            if _CONNECTOR_PLUGINS in path.parents:
                continue
            for line, name in _identifiers(ast.parse(path.read_text())):
                if name in _VENDOR_NAMES or "servicenow" in name.lower():
                    leaks.append(f"{path.relative_to(_REPO)}:{line}: {name}")
    assert not leaks, "vendor names in core code:\n" + "\n".join(leaks)


def _http_connector() -> tuple[ServiceNow, FakeServiceNowHTTP]:
    fake_http = FakeServiceNowHTTP()
    client = httpx.Client(transport=SyncASGITransport(fake_http.app), base_url="http://sn")
    http = ServiceNowHTTP(
        http=client,
        auth=BasicAuth(user="svc_supportagent", password="secret"),  # noqa: S106
        sleep=lambda _s: None,
        jitter=None,
    )
    return ServiceNow(http=http), fake_http


def test_servicenow_connector_contract_unchanged() -> None:
    """ServiceNow is a full ``Connector``, and everything it writes or maps is as in v0.0.

    The behavioural suites (``tests/contract/test_ticket_source_contract.py``,
    ``test_ticket_sink_contract.py``) run unchanged against both the
    in-memory fake and the real adapter; this pins what the rename could
    have moved: the journal fields, the refs, the stored keys and the mapping.
    """
    connector, fake_http = _http_connector()
    as_port: Connector = connector  # the static check is the contract
    assert as_port.manifest == SERVICENOW_MANIFEST
    assert SERVICENOW_MANIFEST.capabilities == frozenset(Capability)
    assert FakeServiceNow().manifest.capabilities == frozenset(Capability)

    fake_http.seed_record("incident", "rec1", number="INC1", updated_at=T0)
    internal = connector.append_journal(RecordType.INCIDENT, "rec1", NoteVisibility.INTERNAL, "a")
    customer = connector.append_journal(RecordType.INCIDENT, "rec1", NoteVisibility.CUSTOMER, "b")
    assert [j["element"] for j in fake_http._journal] == ["work_notes", "comments"]
    assert internal.as_text() == "incident:rec1#work_notes"
    assert customer.as_text() == "incident:rec1#comments"

    # Stored keys unchanged: cursor rows, cases.external_table and scopes use these.
    assert [t.value for t in RecordType] == ["incident", "em_alert"]

    semantics = connector.semantics
    assert semantics.resolved_states == frozenset({"6", "7", "8"})
    assert (semantics.state_field, semantics.assignee_field) == ("state", "assigned_to")
    assert [severity_from_priority(p) for p in ("1", "2", "3", "4", "5", "9", None)] == [
        Severity.P1,
        Severity.P2,
        Severity.P3,
        Severity.P4,
        Severity.P4,
        None,
        None,
    ]
    assert semantics.priority_raised("5", "4")  # Planning to Low is a raise, as before
    assert not semantics.priority_raised("2", "3")
    assert not semantics.priority_raised(None, "1")  # created without a priority
    assert semantics.display("state", "7") == "Closed"
    assert ServiceNowSemantics.with_resolved_states(["100"]).resolved_states == {"100"}
    assert ServiceNowSemantics.with_resolved_states(None) == ServiceNowSemantics()


def test_actor_classification() -> None:
    """ARCH §8.2: an unconfirmed automation candidate still counts as human."""
    kw: dict[str, Any] = {"integration_user": "svc", "automation_actors": {"flow.designer"}}
    assert classify_actor("svc", **kw) is ActorKind.AGENT
    assert classify_actor("system", **kw) is ActorKind.SYSTEM
    assert classify_actor("", **kw) is ActorKind.SYSTEM
    assert classify_actor("flow.designer", **kw) is ActorKind.AUTOMATION
    assert classify_actor("flow.candidate", **kw) is ActorKind.HUMAN


_NO_HISTORY = frozenset(Capability) - {Capability.HISTORY}


def test_missing_history_capability_disables_auto_takeover(uow: Any, clock: Any) -> None:
    """No history: no takeover detection, so no change is delivered and only shadow runs."""
    limits = limits_for(ConnectorManifest("partial", _NO_HISTORY))
    assert not limits.takeover_detection
    assert not limits.autonomous
    assert any("history" in r for r in limits.reasons)

    # The sync service never asks for history (the fake would raise) and
    # delivers nothing for a known record's update: no lifecycle, no takeover.
    connector = FakeServiceNow(capabilities=_NO_HISTORY)
    handler = _RecordingHandler()
    sync = SyncService(
        tenant_id="acme",
        uow=uow,
        repo=SyncRepo(uow),
        source=connector,
        handler=handler,
        clock=clock,
        integration_user="svc_supportagent",
        to_alert=record_to_alert,
        semantics=ServiceNowSemantics(),
        history=limits.takeover_detection,
    )
    _seed(connector, "rec1", "INC1", T0)
    sync.poll_table(RecordType.INCIDENT)
    assert len(handler.alerts) == 1  # first sight still triages
    _seed(connector, "rec1", "INC1", T0 + timedelta(seconds=5), user="alice", mod_count=2)
    sync.poll_table(RecordType.INCIDENT)
    assert handler.changes == []
    assert len(handler.alerts) == 1

    # Startup refuses any mode that acts; shadow still runs.
    shadow = _shadow_config()
    require_connector_supports_mode(shadow, ConnectorManifest("partial", _NO_HISTORY))
    assisted = shadow.model_copy(update={"mode": "assisted"})
    with pytest.raises(ConfigError, match="history"):
        require_connector_supports_mode(assisted, ConnectorManifest("partial", _NO_HISTORY))
    require_connector_supports_mode(assisted, SERVICENOW_MANIFEST)

    # And doctor says so.
    ctx = _ctx(clock=clock, config=assisted)
    partial = dataclasses.replace(ctx, connector=ConnectorManifest("partial", _NO_HISTORY))
    assert ConnectorCheck().run(partial).status == "fail"
    assert ConnectorCheck().run(dataclasses.replace(partial, config=shadow)).status == "warn"
    assert ConnectorCheck().run(ctx).status == "pass"


def test_missing_marker_read_disables_writes() -> None:
    """Without reconciliation (INV-04) nothing is written at all."""
    limits = limits_for(
        ConnectorManifest("partial", frozenset(Capability) - {Capability.MARKER_READ})
    )
    assert not limits.writes
    assert not (limits.internal_notes or limits.customer_notes or limits.field_updates)
    no_comments = limits_for(
        ConnectorManifest("partial", frozenset(Capability) - {Capability.CUSTOMER_NOTES})
    )
    assert no_comments.autonomous
    assert no_comments.internal_notes
    assert not no_comments.customer_notes


def test_connector_doctor_checks_registered() -> None:
    """Each connector's checks run in the default registry, then the generic ``connector`` check."""
    assert set(CONNECTOR_DOCTORS) == {"servicenow", "none"}
    for name, plugin in CONNECTOR_DOCTORS.items():
        assert plugin.name == name
        own = [c.name for c in plugin.checks]
        assert own
        assert all(n.startswith(f"{name}.") for n in own)
        names = [c.name for c in default_checks(name)]
        start = names.index(own[0])
        assert names[start : start + len(own) + 1] == [*own, "connector"]
        assert names.index("db") < start < names.index("automation_actors")
    with pytest.raises(ConfigError, match="no doctor checks"):
        connector_doctor("jira")
