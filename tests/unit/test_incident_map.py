"""WP-O4 (ADR-0015 P3): the incident map: grounding, structure first, classes and weights."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.files.eval_loader import load_yaml_records
from supportagent.adapters.files.taxonomy_loader import load_hypotheses
from supportagent.adapters.servicenow.history_export import past_incidents
from supportagent.app.incident_map import IncidentsAnswer, ReadingAnswer, map_incidents
from supportagent.bootstrap import (
    ONBOARDING_DIR,
    build_incident_map,
    detect_plugins,
    incident_catalogue,
    load_plugins,
    onboard_incidents,
)
from supportagent.config.loader import load_config_offline
from supportagent.domain.incident_map import (
    BUILT_IN_CAUSES,
    PastIncident,
    Reading,
    build_map,
    catalogue,
    ground,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]

_T0 = datetime(2026, 3, 1, 9, tzinfo=UTC)
_CAT = catalogue(
    [("database/saturation", "pools"), ("request/access", "access"), ("deploy/failure", "deploy")],
    [("oracle/lock_contention", "row locks"), ("jvm/connection_pool_exhaustion", "pool")],
)


def _inc(  # noqa: PLR0913 — a builder with keyword defaults
    number: str,
    resolution: str,
    *,
    severity: str = "P3",
    hours: float = 2,
    problem: str | None = None,
    change: str | None = None,
) -> PastIncident:
    return PastIncident(
        number=number,
        opened=_T0,
        resolved=_T0 + timedelta(hours=hours),
        severity=severity,
        ci="payment-service",
        title="Payments slow",
        description="Captures timing out.",
        resolution=resolution,
        problem=problem,
        caused_by_change=change,
    )


def test_a_reading_keeps_its_cause_only_with_a_verbatim_quote() -> None:
    incident = _inc("I1", "DBA killed the blocking session holding row locks on CAPTURES.")
    kept = ground(
        Reading(
            "I1", "database/saturation", "oracle/lock_contention", "killed the blocking session"
        ),
        incident,
        _CAT,
    )
    assert (kept.label, kept.cause, kept.dropped) == (
        "database/saturation",
        "oracle/lock_contention",
        None,
    )
    for reading, why in [
        (Reading("I1", "database/saturation", "oracle/lock_contention", "locks"), "no usable"),
        (
            Reading("I1", None, "oracle/lock_contention", "the DBA restarted it"),
            "not in the ticket",
        ),
        (
            Reading("I1", "database/saturation", "oracle/mystery", "killed the blocking"),
            "catalogue",
        ),
    ]:
        got = ground(reading, incident, _CAT)
        assert got.cause is None and why in str(got.dropped)
    assert ground(Reading("I1", "app/unknown", None), incident, _CAT).label is None


def test_a_problem_record_and_a_change_link_overrule_the_readings() -> None:
    incidents = [
        _inc("I1", "row locks", problem="PRB1"),
        _inc("I2", "row locks", problem="PRB1"),
        _inc("I3", "pool exhausted", problem="PRB1"),  # misread, but a person linked it
        _inc("I4", "rolled back the release", change="CHG9"),
        _inc("I5", "Access granted."),
    ]
    answers = [
        ("database/saturation", "oracle/lock_contention", "row locks"),
        ("database/saturation", "oracle/lock_contention", "row locks"),
        ("database/saturation", "jvm/connection_pool_exhaustion", "pool exhausted"),
        ("deploy/failure", "jvm/connection_pool_exhaustion", "rolled back the release"),
        ("request/access", None, ""),
    ]
    readings = {
        i.number: ground(Reading(i.number, *answer), i, _CAT)
        for i, answer in zip(incidents, answers, strict=True)
    }
    mapped = build_map(incidents, readings)
    by_cause = {c.cause: c for c in mapped.classes}
    assert by_cause["oracle/lock_contention"].incidents == ("I1", "I2", "I3")
    assert by_cause["oracle/lock_contention"].basis == ("problem",)
    assert by_cause["recent_change_push"].incidents == ("I4",)
    assert by_cause["recent_change_push"].changes == ("CHG9",)
    assert by_cause[None].label == "request/access"
    assert mapped.coverage == 0.8


def test_classes_are_weighted_by_count_severity_and_time_to_resolve() -> None:
    incidents = [
        _inc("I1", "row locks", severity="P1", hours=24),  # 8 x 2
        _inc("I2", "pool exhausted", severity="P4", hours=0),  # 1 x 1
        _inc("I3", "pool exhausted", severity="P4", hours=0),
    ]
    readings = {
        i.number: ground(Reading(i.number, "database/saturation", c, i.resolution), i, _CAT)
        for i, c in zip(
            incidents,
            ["oracle/lock_contention", *["jvm/connection_pool_exhaustion"] * 2],
            strict=True,
        )
    }
    first, second = build_map(incidents, readings).classes
    assert (first.cause, first.weight, first.share) == ("oracle/lock_contention", 16.0, 0.889)
    assert (second.weight, second.mean_hours_to_resolve) == (2.0, 0.0)
    assert dict(first.severities) == {"P1": 1}


def test_the_mapper_reads_in_batches_and_ignores_numbers_it_did_not_ask_about() -> None:
    incidents = [_inc(f"I{n:02d}", "row locks") for n in range(10)]
    batches: list[str] = []

    def read(labels: str, causes: str, text: str) -> IncidentsAnswer:
        batches.append(text)
        assert "oracle/lock_contention: row locks" in causes
        numbers = [line.split()[1] for line in text.splitlines() if line.startswith("### ")]
        return IncidentsAnswer(
            incidents=[
                ReadingAnswer(
                    number=n,
                    label="database/saturation",
                    cause="oracle/lock_contention",
                    quote="row locks",
                )
                for n in [*numbers, "I99"]
            ]
        )

    mapped = map_incidents(incidents, _CAT, read)
    assert len(batches) == 2 and mapped.unread == []
    assert [c.cause for c in mapped.classes] == ["oracle/lock_contention"]
    assert "I99" not in mapped.readings
    assert map_incidents(incidents, _CAT, None).unread == [i.number for i in incidents]


def test_the_built_in_causes_are_the_common_hypotheses() -> None:
    common = {h.id for h in load_hypotheses(_REPO / "taxonomy" / "hypotheses.yaml").hypotheses}
    assert set(BUILT_IN_CAUSES) == common


def test_a_system_catalogue_has_only_the_plugins_it_needs() -> None:
    plugins = load_plugins()
    a = ONBOARDING_DIR / "system-a"
    cat = incident_catalogue(detect_plugins(a, plugins), plugins)
    assert "oracle/lock_contention" in cat.causes and "recent_change_push" in cat.causes
    assert not any(c.startswith(("snowflake/", "kafka/")) for c in cat.causes)
    assert "app/memory" in cat.labels and "warehouse/queuing" not in cat.labels


def test_lab_history_reads_as_past_incidents() -> None:
    history = past_incidents(
        [
            {
                "number": "INC1",
                "opened_at": "2026-01-02 03:04:05",
                "resolved_at": "2026-01-02 05:04:05",
                "priority": "2",
                "cmdb_ci": "payment-service",
                "short_description": "Slow",
                "close_notes": "Killed the blocker.",
                "problem_id": "PRB1",
                "caused_by": "CHG1",
            },
            {"number": "INC2", "opened_at": "not a date"},
        ],
        [{"number": "PRB1", "short_description": "Row locks"}],
    )
    [incident] = history
    assert (incident.severity, incident.hours_to_resolve) == ("P2", 2.0)
    assert (incident.problem_title, incident.caused_by_change) == ("Row locks", "CHG1")


def test_without_the_model_the_structure_still_groups_the_history() -> None:
    a = ONBOARDING_DIR / "system-a"
    plugins = load_plugins()
    mapped = build_incident_map(a, detect_plugins(a, plugins), plugins, None)
    change = next(c for c in mapped.classes if c.cause == "recent_change_push")
    assert len(change.incidents) == 6 and change.basis == ("change",)
    assert mapped.coverage < 0.2  # only the change links name a cause


def test_onboard_incidents_writes_the_map_into_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    shutil.copytree(ONBOARDING_DIR / "system-a", workspace)
    [locked] = [
        r for r in load_yaml_records(workspace / "sources/servicenow/incidents.yaml")
        if r["number"] == "INCA0009"
    ]  # fmt: skip
    fake = FakeLLMProvider()
    quote = str(locked["close_notes"])[:60]
    fake.respond(
        json.dumps(
            {"incidents": [{"number": "INCA0009", "label": "database/saturation",
                            "cause": "oracle/lock_contention", "quote": quote}]}
        )
    )  # fmt: skip
    config = load_config_offline(_REPO / "evals/lab/lab.yaml")
    mapped, path = onboard_incidents(config, workspace, provider=fake)
    document = json.loads(path.read_text())
    lock = next(c for c in document["classes"] if c["cause"] == "oracle/lock_contention")
    assert len(lock["incidents"]) == 7 and lock["basis"] == ["problem"]  # the problem record
    assert document["coverage"] == mapped.coverage > 0.2
    assert len(fake.captured) == 6  # 45 incidents in batches of 8
    assert "oracle/lock_contention" in fake.captured[0].system
    assert "<untrusted_tickets>" in fake.captured[0].user
