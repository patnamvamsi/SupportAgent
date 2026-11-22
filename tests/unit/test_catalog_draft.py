"""ADR-0020 §5 follow-up: onboarding drafts the service catalog the graph implies.

Shadow replay found that triage refuses every service of a newly onboarded system:
none is in the catalog, and none has a paging integration. The draft fills in what
onboarding knows and marks what only a person can (the paging integration).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from supportagent.bootstrap import (
    ONBOARDING_DIR,
    _past_incidents,
    _UnrecordedProvider,
    build_system_graph,
    draft_catalog,
    onboard_catalog,
    render_catalog_draft,
)
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.unit


def _draft(system: str, configured: list[ServiceEntry] | None = None) -> list:  # type: ignore[type-arg]
    d = ONBOARDING_DIR / system
    graph = build_system_graph(d, None).graph
    return draft_catalog(graph, [i.ci for i in _past_incidents(d)], configured or [])


def test_every_service_the_history_names_gets_an_entry_from_the_graph() -> None:
    entries = {e.entry.name: e for e in _draft("system-b")}
    assert {"customer-insights", "daily-sales-task", "batch-features", "stream-ingest"} <= set(
        entries
    )
    insights = entries["customer-insights"]
    assert insights.status == "new"
    assert insights.entry.cmdb_ci == ["customer-insights"]
    assert {"streamlit", "snowflake", "kubernetes"} <= set(insights.entry.stack)
    assert insights.entry.k8s_namespace == "analytics-apps"
    assert insights.todo == ("add its paging_integration",)  # the one thing onboarding can't know
    assert all(e.entry.paging_integration is None for e in entries.values())


def test_a_service_already_configured_keeps_its_entry() -> None:
    configured = ServiceEntry(
        name="insights",
        cmdb_ci=["customer-insights"],
        paging_integration="pagerduty:PINS",
        stack=["streamlit"],
    )
    entries = {e.entry.name: e for e in _draft("system-b", [configured])}
    assert "customer-insights" not in entries
    kept = entries["insights"]
    assert (kept.status, kept.entry, kept.todo) == ("kept", configured, ())


def test_the_draft_is_a_catalog_that_loads_with_its_todos_as_comments() -> None:
    entries = _draft("system-c")
    text = render_catalog_draft(entries, system="shipment-tracking")
    catalog = Catalog.model_validate(yaml.safe_load(text))
    assert {s.name for s in catalog.services} == {e.entry.name for e in entries}
    assert text.count("TODO: add its paging_integration") == len(entries)
    assert "# new" in text


def test_onboard_catalog_writes_the_draft_in_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    shutil.copytree(ONBOARDING_DIR / "system-a", workspace)
    entries, path = onboard_catalog(_shadow_config(), workspace, provider=_UnrecordedProvider())
    assert path == workspace / "catalog.proposed.yaml"
    loaded = Catalog.model_validate(yaml.safe_load(path.read_text()))
    assert {"payment-service", "orders-service"} <= {s.name for s in loaded.services}
    assert len(entries) == len(loaded.services)


def test_a_job_is_its_owners_work_not_a_service() -> None:
    """ADR-0023 §3: scheduled jobs found in the code join their service's entry."""
    from supportagent.adapters.files.graph_extract import structured_facts  # noqa: PLC0415
    from supportagent.bootstrap import draft_catalog  # noqa: PLC0415
    from supportagent.domain.graph import build  # noqa: PLC0415
    from tests.unit.test_code_layer import _declared, _system  # noqa: PLC0415

    graph = build(structured_facts(_system(), {}, {}, _declared()))
    entries = {e.entry.name: e.entry for e in draft_catalog(graph, [], [])}
    assert not any("sync-bars" in name for name in entries), "no service per job"
    assert entries["feed"].resources == ["feed", "feed.sync_bars"], "an alert on it finds feed"
