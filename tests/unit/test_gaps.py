"""ADR-0017 (WP-A3): what no plugin covers is reported, with evidence and a next step."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from supportagent.bootstrap import ONBOARDING_DIR, _UnrecordedProvider, onboard_gaps
from supportagent.domain.gaps import (
    technology,
    uncaused_incidents,
    unclaimed_repos,
    unknown_capabilities,
    unmapped_cmdb_classes,
)
from supportagent.domain.graph import Node, SystemGraph
from supportagent.domain.incident_map import GroundedReading, PastIncident, build_map
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.unit


def test_a_manifest_names_its_technology() -> None:
    assert technology("go.mod") == "go"
    assert technology("Billing.csproj") == "dotnet"
    assert technology("README.md") is None


def test_a_repository_nothing_claimed_is_a_gap_with_its_manifests() -> None:
    graph = SystemGraph(
        nodes={
            "service:orders": Node(
                "service:orders", "Service", {"nodejs"}, {"git:orders/package.json"}
            )
        }
    )
    files = ["orders/package.json", "planner/go.mod", "planner/main.go"]
    (gap,) = unclaimed_repos(files, graph)
    assert gap.subject == "planner" and gap.evidence == ("go.mod (go)",)
    assert "supportagent plugin new go --from go.mod" in gap.suggestion


def test_an_installed_ci_class_nothing_maps_is_a_gap() -> None:
    cis = [
        ("route-cache", "u_cmdb_ci_redis", "Installed"),
        ("old-cache", "u_cmdb_ci_redis", "Retired"),
        ("orders", "cmdb_ci_service_auto", "Installed"),
    ]
    (gap,) = unmapped_cmdb_classes(cis, {"cmdb_ci_service_auto"})
    assert gap.subject == "u_cmdb_ci_redis" and gap.evidence == ("route-cache", "1 CI(s)")


def _incident(number: str, title: str) -> PastIncident:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    return PastIncident(number, at, at, "P2", "orders", title, "d", "r")


def test_operational_incidents_with_no_cause_are_a_gap_but_requests_and_unread_are_not() -> None:
    incidents = [
        _incident("I1", "Orders stuck"),
        _incident("I2", "Orders stuck again"),
        _incident("I3", "Grant access"),
        _incident("I4", "Never read"),
    ]
    readings = {
        "I1": GroundedReading("I1", "app/config", None, None),
        "I2": GroundedReading("I2", "app/config", None, None),
        "I3": GroundedReading("I3", "request/access", None, None),
    }
    imap = build_map(incidents, readings)
    imap.unread = ["I4"]
    (gap,) = uncaused_incidents(imap, incidents)
    assert gap.subject == "2 incident(s) with no known cause"
    assert gap.evidence[0] == "on orders (2)" and gap.weight > 0


def test_a_pack_check_on_a_capability_nothing_provides_is_a_gap() -> None:
    (gap,) = unknown_capabilities({"redis": ["redis.memory", "logs.search"]}, {"logs.search"})
    assert (gap.subject, gap.evidence) == ("redis", ("redis.memory",))


def test_a_workspace_reports_a_go_service_and_a_redis_ci_no_plugin_covers(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    shutil.copytree(ONBOARDING_DIR / "system-c", workspace)
    planner = workspace / "sources" / "git" / "route-planner"
    planner.mkdir()
    (planner / "go.mod").write_text("module github.com/acme-logistics/route-planner\n\ngo 1.22\n")
    (planner / "main.go").write_text("package main\n\nfunc main() {}\n")
    cmdb_path = workspace / "sources" / "servicenow" / "cmdb.yaml"
    cmdb = yaml.safe_load(cmdb_path.read_text())
    cmdb["cis"].append(
        {
            "sys_id": "ci-c-rc",
            "name": "route-cache",
            "sys_class_name": "u_cmdb_ci_redis",
            "install_status": "Installed",
        }
    )
    cmdb_path.write_text(yaml.safe_dump(cmdb, sort_keys=False))
    gaps, path = onboard_gaps(_shadow_config(), workspace, provider=_UnrecordedProvider())
    found = {(g.kind, g.subject) for g in gaps}
    assert ("unclaimed_repo", "route-planner") in found
    assert ("unmapped_cmdb_class", "u_cmdb_ci_redis") in found
    assert not any(k == "uncaused_incidents" for k, _ in found)  # unread, not uncaused
    assert path.name == "gaps.json"


def test_plugin_new_scaffolds_a_plugin_that_claims_the_go_repo(tmp_path: Path) -> None:
    from supportagent.adapters.files.graph_extract import structured_facts  # noqa: PLC0415
    from supportagent.adapters.files.plugin_loader import load_manifest  # noqa: PLC0415
    from supportagent.bootstrap import scaffold_plugin  # noqa: PLC0415
    from supportagent.domain.graph import NodeFact  # noqa: PLC0415

    path = scaffold_plugin("go", "go.mod", tmp_path)
    manifest = load_manifest(path.parent)
    assert manifest.status == "skeleton" and manifest.detectors.signals[0].files == "**/go.mod"
    files = {"route-planner/go.mod": "module github.com/acme-logistics/route-planner\n\ngo 1.22\n"}
    facts = structured_facts(files, {}, {}, [("go", e) for e in manifest.provides.extractors])
    assert any(isinstance(f, NodeFact) and f.id == "service:route-planner" for f in facts)
    with pytest.raises(Exception, match="already exists"):
        scaffold_plugin("go", "go.mod", tmp_path)
