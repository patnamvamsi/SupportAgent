"""WP-O8 (ADR-0015 P7): drift findings and the re-onboarding diff."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from supportagent.adapters.files.change_set import apply_change_set
from supportagent.domain.drift import ACTIONS, ClassShape, Finding, Snapshot, diff
from supportagent.domain.errors import ConfigError

pytestmark = pytest.mark.unit

_GW, _CUS, _DB = "service:gateway", "service:customer-service", "database:crmdb"
_BASE = Snapshot(
    plugins=frozenset({"oracle", "java-spring"}),
    nodes={_GW: "Service", _CUS: "Service", _DB: "Database", "runtime:prod": "Runtime"},
    edges=frozenset({(_GW, "calls", _CUS), (_CUS, "uses", _DB), (_CUS, "runs_on", "runtime:prod")}),
    classes={
        "oracle-lock-contention": ClassShape("oracle/lock_contention", "database/latency", 0.4, 7),
        "request-access": ClassShape(None, "request/access", 0.02, 3),
    },
    known_causes=frozenset({"oracle/lock_contention", "kafka/consumer_lag"}),
    requests=frozenset({"oracle/oracle_ro/crmdb"}),
)


def _keys(findings: list[Finding]) -> set[tuple[str, str]]:
    return {f.key for f in findings}


def test_nothing_changed_nothing_found() -> None:
    assert diff(_BASE, _BASE) == []
    assert diff(Snapshot.of(_BASE.as_dict()), _BASE) == []  # the snapshot round-trips


def test_a_rename_is_one_finding_not_a_removal_and_an_addition() -> None:
    renamed = "service:customer-profile"
    now = Snapshot(
        plugins=_BASE.plugins,
        nodes={_GW: "Service", renamed: "Service", _DB: "Database", "runtime:prod": "Runtime"},
        edges=frozenset(
            {(_GW, "calls", renamed), (renamed, "uses", _DB), (renamed, "runs_on", "runtime:prod")}
        ),
        classes=_BASE.classes,
        known_causes=_BASE.known_causes,
        requests=_BASE.requests,
    )
    assert _keys(diff(_BASE, now)) == {("node_renamed", f"{_CUS} -> {renamed}")}


def test_a_node_whose_relationships_differ_is_not_a_rename() -> None:
    now = Snapshot(
        plugins=_BASE.plugins,
        nodes={**_BASE.nodes, "service:billing": "Service"},
        edges=_BASE.edges | {("service:billing", "uses", "database:billing")},
        classes=_BASE.classes,
        known_causes=_BASE.known_causes,
        requests=_BASE.requests,
    )
    keys = _keys(diff(_BASE, now))
    assert ("node_added", "service:billing") in keys
    assert not any(k == "node_renamed" for k, _ in keys)


def test_new_classes_uncovered_causes_growth_plugins_and_requests() -> None:
    now = Snapshot(
        plugins=_BASE.plugins | {"kafka"},
        nodes=_BASE.nodes,
        edges=_BASE.edges,
        classes={
            "oracle-lock-contention": ClassShape(
                "oracle/lock_contention", "database/latency", 0.5, 9
            ),
            "kafka-consumer-lag": ClassShape("kafka/consumer_lag", "stream/lag", 0.1, 6),
            "pdf-fonts": ClassShape("pdf/missing_font", "app/config", 0.03, 3),
            "request-access": ClassShape(None, "request/access", 0.03, 6),
        },
        known_causes=_BASE.known_causes,
        requests=frozenset({"kafka/kafka_ro/payments-msk"}),
    )
    findings = diff(_BASE, now)
    assert _keys(findings) == {
        ("plugin_added", "kafka"),
        ("class_new", "kafka-consumer-lag"),
        ("class_new", "pdf-fonts"),
        ("class_uncovered", "pdf-fonts"),  # no detected plugin declares the cause
        ("class_grew", "oracle-lock-contention"),
        ("uncaused_grew", "request-access"),
        ("request_new", "kafka/kafka_ro/payments-msk"),
        ("request_gone", "oracle/oracle_ro/crmdb"),
    }
    assert all(f.action == ACTIONS[f.kind] for f in findings)
    assert [f.kind for f in findings][:1] == ["plugin_added"]  # plugins, graph, incidents, requests


def test_a_class_that_disappears_or_a_plugin_no_longer_needed() -> None:
    now = Snapshot(
        plugins=frozenset({"java-spring"}),
        nodes=_BASE.nodes,
        edges=_BASE.edges,
        classes={"request-access": _BASE.classes["request-access"]},
        known_causes=_BASE.known_causes,
        requests=_BASE.requests,
    )
    assert _keys(diff(_BASE, now)) == {
        ("plugin_removed", "oracle"),
        ("class_gone", "oracle-lock-contention"),
    }


def test_a_change_set_writes_a_changed_copy_and_never_touches_the_system(tmp_path: Path) -> None:
    system = tmp_path / "system"
    (system / "sources" / "git" / "old").mkdir(parents=True)
    (system / "sources" / "git" / "old" / "a.txt").write_text("x", encoding="utf-8")
    (system / "list.yaml").write_text("- 1\n", encoding="utf-8")
    (system / "doc.yaml").write_text("cis: []\nname: alpha\n", encoding="utf-8")
    changes = {
        "remove": ["sources/git/old"],
        "files": {"sources/git/new/b.txt": "y"},
        "replace": [{"file": "doc.yaml", "old": "alpha", "new": "beta"}],
        "append": {"list.yaml": [2], "doc.yaml#cis": [{"name": "c"}]},
    }
    out = apply_change_set(system, changes, tmp_path / "out")
    assert (
        not (out / "sources/git/old").exists()
        and (out / "sources/git/new/b.txt").read_text() == "y"
    )
    assert yaml.safe_load((out / "list.yaml").read_text()) == [1, 2]
    assert yaml.safe_load((out / "doc.yaml").read_text()) == {
        "cis": [{"name": "c"}],
        "name": "beta",
    }
    assert (system / "sources/git/old/a.txt").exists()  # the original is untouched
    with pytest.raises(ConfigError, match="has no"):
        apply_change_set(
            system, {"replace": [{"file": "doc.yaml", "old": "zeta", "new": ""}]}, tmp_path / "o2"
        )
    with pytest.raises(ConfigError, match="outside"):
        apply_change_set(system, {"files": {"../escape.txt": "x"}}, tmp_path / "o3")


def test_onboard_drift_accepts_the_first_run_then_reports_and_accepts_a_change(
    tmp_path: Path,
) -> None:
    from supportagent.bootstrap import (  # noqa: PLC0415
        ONBOARDING_DIR,
        _UnrecordedProvider,
        changed_system,
        load_drift_case,
        onboard_drift,
    )
    from supportagent.config.loader import load_config_offline  # noqa: PLC0415

    cfg = load_config_offline(Path(__file__).resolve().parents[2] / "evals/lab/lab.yaml")
    system = ONBOARDING_DIR / "system-a"
    workspace = changed_system(system, {}, tmp_path / "ws")  # a copy: the model's layers off
    offline = _UnrecordedProvider()
    findings, accepted, path = onboard_drift(cfg, workspace, provider=offline)
    assert (findings, accepted) == ([], True) and (workspace / "onboarding_snapshot.json").is_file()
    case = load_drift_case(system)
    assert case is not None
    later = changed_system(workspace, case, tmp_path / "later")
    (later / "onboarding_snapshot.json").write_text(
        (workspace / "onboarding_snapshot.json").read_text(), encoding="utf-8"
    )
    findings, accepted, path = onboard_drift(cfg, later, provider=offline)
    keys = {f.key for f in findings}
    assert ("plugin_added", "kafka") in keys and not accepted
    assert ("node_renamed", "service:customer-service -> service:customer-profile") in keys
    assert '"accepted": false' in path.read_text()
    onboard_drift(cfg, later, provider=offline, accept=True)
    assert onboard_drift(cfg, later, provider=offline)[0] == []
