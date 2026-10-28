"""ADR-0014 part 2 (BL-054): auto-grant conditions, revert declarations, retraction."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from supportagent.adapters.fakes.clock import FrozenClock
from supportagent.app.propose import rca_page_spec, retraction_spec
from supportagent.bootstrap import list_sop_steps, request_sop_auto
from supportagent.domain.auto import (
    REVERTS,
    AutoConditions,
    AutoEvidence,
    auto_refusals,
    reason,
)
from supportagent.domain.errors import ConfigError, DomainValidationError
from supportagent.domain.kinds import CUSTOMER_FACING_KINDS, ActionKind
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.sop import SOP_KINDS

pytestmark = pytest.mark.unit

_EVIDENCE = AutoEvidence(
    service="payroll-api",
    positive=frozenset({"recent_change_push", "db_locks"}),
    confirmed=True,
    rca_artifact="rca_1",
)


def test_conditions_need_a_check_and_a_bounded_cap() -> None:
    with pytest.raises(DomainValidationError, match="at least one check"):
        AutoConditions(checks=(), max_per_day=1)
    for cap in (0, 51):
        with pytest.raises(DomainValidationError, match="max_per_day"):
            AutoConditions(checks=("x",), max_per_day=cap)
    conditions = AutoConditions(checks=("b", "a", "a"), max_per_day=2, services=("s",))
    assert conditions.to_json() == '{"checks":["a","b"],"max_per_day":2,"services":["s"]}'
    assert AutoConditions.from_json(conditions.to_json()) == AutoConditions(
        checks=("a", "b"), max_per_day=2, services=("s",)
    )


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("[1]", "not an object"),
        ("{", "not JSON"),
        ('{"checks":["a"],"max_per_day":1,"window":"9-5"}', "unknown condition"),
        ('{"checks":["a"],"max_per_day":1,"min_score":0.8}', "calibrated thresholds"),
        ('{"checks":"a","max_per_day":1}', "malformed"),
        ('{"checks":["a"],"max_per_day":"1"}', "malformed"),
        ('{"checks":[""],"max_per_day":1}', "malformed"),
        ('{"checks":[],"max_per_day":1}', "at least one check"),
    ],
)
def test_stored_conditions_that_cant_be_read_are_refused(raw: str, why: str) -> None:
    with pytest.raises(DomainValidationError, match=why) as caught:
        AutoConditions.from_json(raw)
    assert why in reason(caught.value) and "\n" not in reason(caught.value)


def test_only_internal_kinds_that_declare_a_revert_can_run_on_auto() -> None:
    assert set(REVERTS) == {ActionKind.WORK_NOTE, ActionKind.ENRICH_PAGE}
    assert set(REVERTS) >= SOP_KINDS  # every kind an SOP may propose can be undone
    assert not set(REVERTS) & CUSTOMER_FACING_KINDS
    conditions = AutoConditions(checks=("recent_change_push",), max_per_day=3)
    for kind in (ActionKind.JOB_RERUN, ActionKind.TICKET_UPDATE, ActionKind.CUSTOMER_COMMENT):
        assert auto_refusals(kind, conditions, _EVIDENCE, used_today=0) == [
            f"{kind.value} declares no revert"
        ]


def test_every_condition_is_checked_and_each_failure_named() -> None:
    kind = ActionKind.WORK_NOTE
    ok = AutoConditions(checks=("recent_change_push",), max_per_day=3, services=("payroll-api",))
    assert auto_refusals(kind, ok, _EVIDENCE, used_today=2) == []
    assert auto_refusals(kind, ok, None, used_today=0) == ["no evidence to evaluate the conditions"]
    bad = AutoConditions(checks=("disk_full", "db_locks"), max_per_day=1, services=("billing",))
    unconfirmed = AutoEvidence("payroll-api", _EVIDENCE.positive, False, "rca_1")
    assert auto_refusals(kind, bad, unconfirmed, used_today=1) == [
        "checks not positive: ['disk_full']",
        "the cause's claim did not pass the evidence check",
        "service 'payroll-api' not in ['billing']",
        "rate cap reached: 1 of 1 today",
    ]


def _sent(kind: ActionKind, **extra: object) -> ActionSpec:
    return ActionSpec(
        kind=kind,
        target=TargetRef(service="payroll-api", system="servicenow", ref="sys-1"),
        scope=f"sop:wiki:sop.md@abc:roll-back:{kind.value}",
        payload={
            "template_id": "sop_step_v1",
            "sop_doc": "wiki:sop.md",
            "sop_step": "roll-back",
            "text": "SOP step ...",
            "revert": "retraction_note",
            **extra,
        },
    )


def test_a_retraction_is_a_fixed_note_of_the_same_kind_on_the_same_record() -> None:
    note = retraction_spec(_sent(ActionKind.WORK_NOTE), proposal_id="prop_9", actor_id="user_ada")
    assert note.kind is ActionKind.WORK_NOTE and note.target.ref == "sys-1"
    assert note.scope == "retract:prop_9:work_note"
    assert note.payload == {
        "template_id": "retraction_v1",
        "retracts": "prop_9",
        "text": (
            "Retraction: the SupportAgent note from SOP wiki:sop.md step roll-back "
            "(proposal prop_9) was posted in auto mode and is withdrawn; disregard it. "
            "Retracted by user_ada."
        ),
    }
    page = retraction_spec(
        _sent(ActionKind.ENRICH_PAGE, integration="pagerduty:payroll", source_ref="sys-1"),
        proposal_id="prop_9",
        actor_id="user_ada",
    )
    assert page.payload["integration"] == "pagerduty:payroll"
    assert page.payload["source_ref"] == "sys-1"
    plain = _sent(ActionKind.WORK_NOTE).model_copy(update={"payload": {"text": "x"}})
    with pytest.raises(DomainValidationError, match="declares no retraction"):
        retraction_spec(plain, proposal_id="prop_9", actor_id="user_ada")


def test_the_page_note_counts_auto_steps_separately() -> None:
    spec = rca_page_spec(
        case_id="case_1",
        service="payroll-api",
        external_id="sys-1",
        integration="pagerduty:payroll",
        outcome="SUPPORTED",
        supported="recent_change_push",
        confirmed_by=["gha.recent_runs"],
        tested=["recent_change_push"],
        sop_pending=1,
        sop_auto=2,
    )
    assert str(spec.payload["text"]).endswith(
        "1 SOP step(s) await approval in SupportAgent. "
        "2 SOP step(s) ran in auto mode; each can be retracted."
    )


def test_sop_list_shows_each_step_scope_and_auto_needs_a_current_one(tmp_path: Path) -> None:
    from tests.unit.test_doctor import _ctx  # noqa: PLC0415

    (tmp_path / "sop.md").write_text(
        "---\ntitle: Deploy SOP\nversion: '2'\nsop:\n  when: [recent_change_push]\n"
        "  steps:\n    - {id: roll-back, kind: work_note, note: Ask the deployer to roll back.}\n"
        "---\n# Deploy SOP\nRoll back first.\n"
    )
    base = _ctx(clock=FrozenClock(start=datetime(2026, 10, 29, tzinfo=UTC))).config
    knowledge = base.knowledge.model_validate(
        {"sources": [{"kind": "git_markdown", "space": "wiki", "path": str(tmp_path)}]}
    )
    config = base.model_copy(update={"knowledge": knowledge})

    [step] = list_sop_steps(config)
    assert step.scope.startswith("sop:wiki:sop.md@") and step.scope.endswith(":roll-back:work_note")
    assert (step.title, step.version, step.when, step.stale) == (
        "Deploy SOP",
        "2",
        ("recent_change_push",),
        False,
    )
    conditions = AutoConditions(checks=("recent_change_push",), max_per_day=1)
    stale_scope = step.scope.replace("@", "@000000000000")  # a hash from an older SOP text
    with pytest.raises(ConfigError, match="not a step of a current SOP"):
        request_sop_auto(config, stale_scope, conditions, master_key=b"k" * 32)
