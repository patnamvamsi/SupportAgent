"""WP-102: comms plan domain rules (ARCH §6.3; 01 §7.1, §7.12).

Canonical test IDs from 11-wp §WP-102 that live here:
``test_plan_rejects_agent_source_field``, ``test_plan_hash_changes_when_template_changes``,
``test_render_rejects_unknown_variable``. The cadence ones run end to end in
``tests/integration/test_wp102_comms.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from supportagent.adapters.files.comms_loader import load_comms_catalog
from supportagent.app.comms import display_value, human_record_state
from supportagent.domain.comms import AllowedField, PlanSpec, interval_slot, render
from supportagent.domain.errors import ConfigError, DomainValidationError
from supportagent.domain.hashing import payload_hash
from supportagent.domain.model.ticket import ChangeRecord

_REPO = Path(__file__).resolve().parents[2]


def _plan(template_id: str = "customer_update_v1", cadence: int | None = 240) -> PlanSpec:
    return PlanSpec(
        audience="customer",
        channel="servicenow_comment",
        template_id=template_id,
        allowed_fields=(
            AllowedField("state", "record_state"),
            AllowedField("next_update_eta", "schedule"),
        ),
        cadence_minutes=cadence,
    )


def test_plan_rejects_agent_source_field() -> None:
    with pytest.raises(DomainValidationError, match="never 'agent'"):
        AllowedField("state", "agent")  # type: ignore[arg-type]
    with pytest.raises(DomainValidationError, match="schedule fields"):
        AllowedField("resolution", "schedule")


def test_plan_hash_changes_when_template_changes() -> None:
    base = _plan()
    assert base.plan_hash() != _plan(template_id="customer_update_v2").plan_hash()
    assert base.plan_hash() != _plan(cadence=60).plan_hash()
    # The plan proposal's payload hashes to the plan_hash: approving it
    # approves exactly this plan.
    assert payload_hash(base.payload()) == base.plan_hash()


def test_render_rejects_unknown_variable() -> None:
    allowed = {"state": "record_state"}
    assert render("Status: {{ state }}.", {"state": "Resolved"}, allowed) == "Status: Resolved."
    with pytest.raises(DomainValidationError, match="outside the plan"):
        render("Cause: {{ root_cause }}", {"root_cause": "x"}, allowed)
    with pytest.raises(DomainValidationError, match="no value for state"):
        render("Status: {{ state }}", {}, allowed)
    # Nothing but substitution is interpreted.
    assert render("{{ state }} {% if x %}", {"state": "New"}, allowed) == "New {% if x %}"


def test_plans_are_customer_plans_on_a_known_channel() -> None:
    with pytest.raises(DomainValidationError, match="customer plans"):
        PlanSpec(
            audience="internal",
            channel="servicenow_comment",
            template_id="t",
            allowed_fields=(),
            cadence_minutes=60,
        )
    with pytest.raises(DomainValidationError, match="unknown channel"):
        PlanSpec(
            audience="customer",
            channel="sms",
            template_id="t",
            allowed_fields=(),
            cadence_minutes=60,
        )


def test_interval_slot_floor_and_first_slot_zero() -> None:
    t0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    assert interval_slot(t0, t0, 240) == 0
    assert interval_slot(t0 + timedelta(minutes=239), t0, 240) == 0
    assert interval_slot(t0 + timedelta(minutes=240), t0, 240) == 1
    assert interval_slot(t0 + timedelta(hours=13), t0, 240) == 3


def test_record_state_is_the_last_human_value() -> None:
    t0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    changes = [
        ChangeRecord(source="audit", user="bob", ts=t0, fieldname="state", newvalue="2"),
        ChangeRecord(
            source="audit",
            user="svc",
            ts=t0 + timedelta(minutes=1),
            fieldname="state",
            newvalue="6",
        ),  # the agent's write: never a source
        ChangeRecord(
            source="audit",
            user="flow.designer",
            ts=t0 + timedelta(minutes=2),
            fieldname="priority",
            newvalue="1",
        ),  # confirmed automation: not human
        ChangeRecord(
            source="audit",
            user="carol",
            ts=t0 + timedelta(minutes=3),
            fieldname="priority",
            newvalue="2",
        ),
    ]
    state = human_record_state(changes, integration_user="svc", automation_actors={"flow.designer"})
    assert state == {"state": "2", "priority": "2"}
    assert display_value("state", "6") == "Resolved"
    assert display_value("priority", "2") == "2 - High"


def test_shipped_comms_catalog_loads() -> None:
    catalog = load_comms_catalog(_REPO / "templates" / "comms")
    spec = catalog.plans["customer-4h"]
    assert spec.cadence_minutes == 240
    assert "{{ state }}" in catalog.templates[spec.template_id]


def test_catalog_rejects_template_outside_allowed_fields(tmp_path: Path) -> None:
    (tmp_path / "plans.yaml").write_text(
        "plans:\n  p:\n    audience: customer\n    channel: servicenow_comment\n"
        "    template_id: t\n    cadence_minutes: 60\n    allowed_fields: {state: record_state}\n",
        encoding="utf-8",
    )
    (tmp_path / "t.txt").write_text("{{ state }} because {{ cause }}", encoding="utf-8")
    with pytest.raises(ConfigError, match="cause"):
        load_comms_catalog(tmp_path)
