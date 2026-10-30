"""WP-O1 (ADR-0015 §2): plugin manifests, the pinned registry, and detection."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from supportagent.adapters.files.plugin_loader import load_registry, write_index
from supportagent.bootstrap import ONBOARDING_DIR, PLUGINS_DIR, load_plugins, snapshot_from_sources
from supportagent.domain.errors import ConfigError
from supportagent.domain.model.plugin import PluginManifest
from supportagent.domain.plugins import DocPage, IncidentText, SourceSnapshot, detect, detected_ids
from supportagent.entrypoints.cli.main import app

pytestmark = pytest.mark.unit


def _manifest(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "demo",
        "version": "0.1.0",
        "description": "A demo plugin.",
        "core_api": ">=1.0,<2",
        "provides": {
            "capabilities": {"demo.read": {"description": "Read the demo"}},
            "backends": [{"capability": "demo.*", "first_party": "demo", "access": "demo_ro"}],
        },
        "requires": {
            "demo_ro": {
                "description": "Read only.",
                "grant": "SELECT on DEMO_VIEW",
                "target": "Database",
                "verify_can": ["select"],
                "verify_cannot": ["insert"],
            }
        },
        "detectors": {"signals": [{"files": "**/demo.cfg"}]},
    }
    return {**base, **over}


def test_a_valid_manifest_loads() -> None:
    m = PluginManifest.model_validate(_manifest())
    assert m.trust == "T0-read" and m.status == "skeleton"


def _grant(text: str) -> dict[str, Any]:
    return {"requires": {"demo_ro": {**_manifest()["requires"]["demo_ro"], "grant": text}}}


_UNDECLARED = {
    "capabilities": {},
    "backends": [{"capability": "x.*", "mcp": {"image": "i", "tools": ["t"]}}],
}


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (_grant("GRANT DBA TO x"), "more than read-only"),
        (_grant("role ACCOUNTADMIN"), "more than read-only"),
        (_grant("SELECT, UPDATE on T"), "more than read-only"),
        ({"publisher": "third-party"}, "never in-process code"),
        ({"trust": "T1"}, "T0-read"),
        ({"core_api": ">=2.0,<3"}, "doesn't admit this core"),
        ({"provides": _UNDECLARED}, "serves no declared capability"),
        ({"detectors": {"signals": [{"files": "a", "incidents": "b"}]}}, "exactly one kind"),
        ({"detectors": {"signals": [{"incidents": "(unclosed"}]}}, "bad regex"),
    ],
)
def test_a_manifest_that_adds_authority_or_is_malformed_is_refused(
    change: dict[str, Any], why: str
) -> None:
    with pytest.raises(ValidationError, match=why):
        PluginManifest.model_validate(_manifest(**change))


def test_a_third_party_plugin_may_ship_an_mcp_server() -> None:
    mcp = {"capability": "demo.*", "mcp": {"image": "x/demo:1", "tools": ["read"]}}
    m = PluginManifest.model_validate(
        _manifest(
            publisher="third-party",
            provides={"capabilities": {"demo.read": {"description": "d"}}, "backends": [mcp]},
        )
    )
    assert m.provides.backends[0].mcp is not None


def test_every_shipped_plugin_loads_pinned_and_the_index_is_current(tmp_path: Path) -> None:
    plugins = load_plugins()
    assert len(plugins) >= 13
    assert all(p.manifest.trust == "T0-read" for p in plugins)
    copy = tmp_path / "plugins"
    shutil.copytree(PLUGINS_DIR, copy)
    write_index(copy)
    assert (copy / "index.yaml").read_text() == (PLUGINS_DIR / "index.yaml").read_text(), (
        "plugins/index.yaml is stale: review the change, then `supportagent plugin index`"
    )


def test_a_changed_or_unpinned_manifest_does_not_load(tmp_path: Path) -> None:
    copy = tmp_path / "plugins"
    shutil.copytree(PLUGINS_DIR, copy)
    manifest = copy / "oracle" / "plugin.yaml"
    manifest.write_text(manifest.read_text().replace("Oracle Database", "Oracle DB"))
    with pytest.raises(ConfigError, match="changed since it was pinned"):
        load_registry(copy)
    write_index(copy)
    load_registry(copy)  # re-pinned after review: loads
    extra = copy / "sneaky"
    extra.mkdir()
    (extra / "plugin.yaml").write_text((copy / "oracle" / "plugin.yaml").read_text())
    with pytest.raises(ConfigError, match=r"not in index\.yaml"):
        load_registry(copy)


def test_a_manifest_must_match_its_directory(tmp_path: Path) -> None:
    copy = tmp_path / "plugins"
    shutil.copytree(PLUGINS_DIR, copy)
    shutil.move(copy / "oracle", copy / "oracle2")
    with pytest.raises(ConfigError, match="must match its directory"):
        write_index(copy)


def _plugin(signals: list[dict[str, Any]], min_score: float = 1.0) -> PluginManifest:
    return PluginManifest.model_validate(
        _manifest(provides={}, requires={}, detectors={"signals": signals, "min_score": min_score})
    )


def test_documentation_counts_only_when_current() -> None:
    plugin = _plugin([{"docs": "redshift", "min_pages": 1}])
    old = DocPage("1", "2024 design (superseded)", frozenset({"architecture"}), "Redshift")
    legacy = DocPage("2", "Old flow", frozenset({"legacy"}), "Redshift cluster")
    now = DocPage("3", "Overview", frozenset({"architecture"}), "We use Redshift")
    assert detected_ids(detect([plugin], SourceSnapshot(docs=[old, legacy]))) == []
    assert detected_ids(detect([plugin], SourceSnapshot(docs=[old, now]))) == ["demo"]


def test_signals_add_up_to_the_threshold_and_explain_themselves() -> None:
    plugin = _plugin(
        [
            {"incidents": "ORA-\\d{5}", "min_share": 0.25, "weight": 0.5},
            {"changes_caused": 0.5, "weight": 0.5},
            {"files": "**/app.yml", "contains": "jdbc:oracle", "weight": 0.5},
        ]
    )
    incidents = [
        IncidentText("I1", "ORA-00060 deadlock", caused_by_change=True),
        IncidentText("I2", "slow page", caused_by_change=True),
        IncidentText("I3", "timeout"),
        IncidentText("I4", "ORA-01653"),
    ]
    files = {"svc/app.yml": "url: jdbc:oracle:thin:@db", "app.yml": "none"}
    [d] = detect([plugin], SourceSnapshot(files=files, incidents=incidents))
    assert d.detected and d.score == 1.5
    assert "2 of 4 incidents match" in d.explain()
    assert "2 of 4 incidents were caused by a change" in d.explain()
    assert "svc/app.yml" in d.explain()
    [weak] = detect([plugin], SourceSnapshot(incidents=incidents[2:3]))
    assert not weak.detected and weak.explain().endswith("no signal fired")


def test_a_double_star_glob_matches_files_at_the_top_too() -> None:
    plugin = _plugin([{"files": "**/pom.xml"}])
    assert detected_ids(detect([plugin], SourceSnapshot(files={"pom.xml": ""}))) == ["demo"]


def test_each_reference_system_detects_exactly_its_plugins() -> None:
    """WP-O1's done-when: the lab's plugin stage passes on real detection."""
    plugins = [p.manifest for p in load_plugins()]
    a = detected_ids(detect(plugins, snapshot_from_sources(ONBOARDING_DIR / "system-a")))
    b = detected_ids(detect(plugins, snapshot_from_sources(ONBOARDING_DIR / "system-b")))
    assert sorted(a) == ["angular", "github", "grafana", "java-spring", "kubernetes", "oracle"]
    assert sorted(b) == [
        "airflow",
        "data-quality",
        "kafka",
        "kubernetes",
        "snowflake",
        "spark",
        "streamlit",
    ]


def test_detection_ignores_the_superseded_pages_and_their_technology() -> None:
    """System A's 2023 page names MySQL and WebLogic; B's 2024 page names Redshift."""
    snap = snapshot_from_sources(ONBOARDING_DIR / "system-b")
    legacy = [p for p in snap.docs if not p.current]
    assert {p.page_id for p in legacy} == {"30002", "30003"}  # superseded design, draft


def test_the_cli_lists_shows_validates_and_detects() -> None:
    runner = CliRunner()
    listed = runner.invoke(app, ["plugin", "list"])
    assert listed.exit_code == 0 and "oracle_ro" in listed.output
    shown = runner.invoke(app, ["plugin", "show", "snowflake"])
    assert shown.exit_code == 0 and "ACCOUNT_USAGE" in shown.output
    assert runner.invoke(app, ["plugin", "show", "nope"]).exit_code == 2
    assert runner.invoke(app, ["plugin", "validate"]).exit_code == 0
    detected = runner.invoke(app, ["onboard", "detect", str(ONBOARDING_DIR / "system-a")])
    assert detected.exit_code == 0
    assert detected.output.count("needs  ") == 6
    assert "the code is on github" in detected.output
