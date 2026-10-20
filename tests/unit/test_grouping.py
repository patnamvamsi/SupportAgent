"""WP-111: storm grouping and the spend breaker (ARCH §4.3, §11; 01 §7.8).

Canonical test IDs from 11-wp §WP-111: ``test_group_join_same_service``,
``test_group_join_dependency_edge``, ``test_group_join_shared_source_threshold``,
``test_storm_mode_opens_above_rate``, ``test_group_one_triage_and_one_rca``
(the triage half; RCA lands with WP-110), ``test_group_closes_when_members_resolved``,
``test_spend_breaker_blocks_llm_and_safe_defaults``,
``test_spend_breaker_cannot_be_disabled_by_config``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.app.grouping import GroupingService
from supportagent.app.spend import SpendBreaker
from supportagent.domain.errors import ConfigError
from supportagent.domain.events import EventKind
from supportagent.domain.grouping import (
    IDLE_CLOSE_S,
    GroupMember,
    OpenGroup,
    assign_group,
    should_close,
)
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.model.event import Event
from supportagent.domain.model.triage import SafeDefaulted, Triaged
from tests.unit.test_triage_service import _evt, _llm_json, _shadow_config, _wire

T = 1_790_000_000


def _svc(name: str, *, depends_on: list[str] | None = None) -> ServiceEntry:
    return ServiceEntry(
        name=name,
        resources=[f"{name}-api"],
        depends_on=depends_on or [],
        paging_integration=f"pagerduty:{name}",
    )


CATALOG = Catalog(
    version="1",
    services=[_svc("payments"), _svc("billing", depends_on=["payments"]), _svc("search")],
)


def _group(*members: GroupMember, opened: int = T) -> OpenGroup:
    return OpenGroup(
        id="grp_1",
        opened_ts=opened,
        lead_case_id=members[0].case_id,
        members=list(members),
        last_member_ts=opened,
    )


def _assign(
    group: OpenGroup | None,
    *,
    service: str,
    source: str = "servicenow",
    recent: int = 0,
    now: int = T + 60,
) -> Any:
    return assign_group(
        service=service,
        source=source,
        now_ts=now,
        open_groups=[group] if group else [],
        recent_new_cases=recent,
        catalog=CATALOG,
    )


# --- domain ------------------------------------------------------------------


def test_group_join_same_service() -> None:
    d = _assign(_group(GroupMember("c1", "payments", "servicenow")), service="payments")
    assert (d.action, d.rule) == ("join", "same_service")
    assert not d.storm_member, "outside storm mode a joined case is still triaged"


def test_group_join_dependency_edge() -> None:
    d = _assign(_group(GroupMember("c1", "payments", "x")), service="billing", source="y")
    assert (d.action, d.rule) == ("join", "dependency")
    d2 = _assign(_group(GroupMember("c1", "billing", "x")), service="payments", source="y")
    assert d2.rule == "dependency", "either direction"
    assert (
        _assign(_group(GroupMember("c1", "payments", "x")), service="search", source="y").action
        == "none"
    )


def test_group_join_shared_source_threshold() -> None:
    four = [GroupMember(f"c{i}", "search", "nagios") for i in range(4)]
    assert _assign(_group(*four), service="payments", source="nagios").action == "none"
    five = [*four, GroupMember("c5", "search", "nagios")]
    d = _assign(_group(*five), service="payments", source="nagios")
    assert (d.action, d.rule) == ("join", "shared_source")


def test_storm_mode_opens_above_rate() -> None:
    assert _assign(None, service="payments", recent=20).action == "none"
    d = _assign(None, service="payments", recent=21)
    assert (d.action, d.rule, d.storm) == ("open", "storm", True)
    member = _assign(_group(GroupMember("c1", "payments", "s")), service="payments", recent=25)
    assert member.storm_member
    stale = _group(GroupMember("c1", "payments", "s"), opened=T - 3600)
    assert _assign(stale, service="payments", recent=25).action == "open", (
        "a group older than the 10-minute window is not a candidate"
    )


def test_group_closes_when_members_resolved() -> None:
    open_member = GroupMember("c1", "payments", "s")
    assert not should_close(_group(open_member), T + 60)
    assert should_close(_group(GroupMember("c1", "payments", "s", resolved=True)), T + 60)
    assert should_close(_group(open_member), T + IDLE_CLOSE_S), "idle for 24 h"


# --- through triage ----------------------------------------------------------


def _seed_recent_cases(db: Any, n: int, now_ts: int) -> None:
    with db.begin() as conn:
        for i in range(n):
            conn.execute(
                text(
                    "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, trace_id, "
                    "duplicate_count, opened_ts, last_alert_ts) VALUES (:id, 'acme', :fp, "
                    "'alert', 'triaged', :tr, 0, :ts, :ts)"
                ),
                {"id": f"case_bg{i}", "fp": f"fp_bg{i}", "tr": f"tr_bg{i}", "ts": now_ts},
            )


def test_group_one_triage_and_one_rca(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    fake_llm.respond(_llm_json("payments/api", "P3"))
    grouping = GroupingService(uow=uow, ids=ids, catalog=CATALOG, tenant_id="acme")
    w = _wire(uow, clock, ids, aead, fake_llm, CATALOG, grouping=grouping)
    _seed_recent_cases(db, 21, int(clock.now().timestamp()))  # a storm is under way
    lead = w.triage.handle(_evt(resource="payments-api", error_signature="sig:lead"))
    assert len(fake_llm.captured) == 1
    members = [
        w.triage.handle(_evt(resource=res, error_signature=f"sig:{i}"))
        for i, res in enumerate(["payments-api", "billing-api", "payments-api", "billing-api"])
    ]
    assert len(fake_llm.captured) == 1, "one LLM classification for the whole group"
    assert all(isinstance(m, SafeDefaulted) for m in members)
    assert {m.decision.reason for m in members} == {"storm-member"}  # type: ignore[union-attr]
    first = members[0]
    assert isinstance(first, SafeDefaulted) and first.decision.llm_suggestion is not None
    assert first.decision.classification == "payments/api", "the lead's labels as a suggestion"
    with db.connect() as conn:
        groups = conn.execute(text("SELECT id, member_count, rule FROM case_groups")).all()
        grouped = conn.execute(
            text("SELECT COUNT(*) FROM cases WHERE group_id IS NOT NULL")
        ).scalar_one()
    assert len(groups) == 1 and groups[0][1:] == (5, "storm")
    assert grouped == 5
    assert not isinstance(lead, SafeDefaulted) or lead.decision.reason != "storm-member"
    # The unrelated service is not pulled into the group.
    other = w.triage.handle(
        _evt(resource="search-api", alert_type="latency", error_signature="sig:search")
    )
    assert not (isinstance(other, SafeDefaulted) and other.decision.reason == "storm-member")


def test_groups_close_job(db: Any, uow: Any, clock: Any, ids: Any) -> None:
    grouping = GroupingService(uow=uow, ids=ids, catalog=CATALOG, tenant_id="acme")
    now = clock.now()
    _seed_recent_cases(db, 21, int(now.timestamp()))
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, "
                "trace_id, duplicate_count, opened_ts, last_alert_ts, service) VALUES "
                "('case_x', 'acme', 'fp_x', 'alert', 'triaged', 'tr_x', 0, :ts, :ts, "
                "'payments')"
            ),
            {"ts": int(now.timestamp())},
        )
    with uow:
        out = grouping.assign("case_x", service="payments", source="servicenow", now=now)
        uow.commit()
    assert out.decision.action == "open"
    assert grouping.close_due(now) == 0
    with db.begin() as conn:
        conn.execute(text("UPDATE cases SET status = 'resolved' WHERE id = 'case_x'"))
    assert grouping.close_due(now) == 1


def _spend_tokens(w: Any, uow: Any, clock: Any, tokens: int) -> None:
    with uow:
        w.events.append(
            Event(
                id="evt_spent",
                tenant_id="acme",
                trace_id="tr_spend",
                span_id="s",
                parent_span_id=None,
                kind=EventKind.LLM,
                actor="agent",
                tokens_in=tokens,
                tokens_out=0,
                payload={"v": 1},
                ts=clock.now(),
            )
        )
        uow.commit()


def test_spend_breaker_blocks_llm_and_safe_defaults(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    fake_llm.respond(_llm_json())
    spend = SpendBreaker(uow=uow, daily_budget_tokens=1_000)  # the triage UoW, as wired
    w = _wire(uow, clock, ids, aead, fake_llm, spend=spend)
    assert isinstance(w.triage.handle(_evt(error_signature="sig:under")), Triaged)
    _spend_tokens(w, uow, clock, 1_000)
    blocked = w.triage.handle(_evt(error_signature="sig:over"))
    assert isinstance(blocked, SafeDefaulted)
    assert blocked.decision.reason == "spend-breaker"
    assert len(fake_llm.captured) == 1, "no LLM call once the budget is spent"
    with db.connect() as conn:
        kinds = conn.execute(text("SELECT COUNT(*) FROM events WHERE kind = 'spend_breaker'"))
        assert kinds.scalar_one() == 1


def test_spend_breaker_cannot_be_disabled_by_config(db: Any) -> None:
    with pytest.raises(ConfigError, match="can't be disabled"):
        SpendBreaker(uow=SqlUnitOfWork(db), daily_budget_tokens=0)
    data = _shadow_config().model_dump(mode="json")
    for value in (0, -1, None):
        data["llm"]["daily_budget_tokens"] = value
        with pytest.raises(ValidationError):
            type(_shadow_config()).model_validate(data)
    for key in ("spend_breaker", "spend_breaker_enabled", "disable_spend_breaker"):
        data["llm"]["daily_budget_tokens"] = 1
        data["llm"][key] = False
        with pytest.raises(ValidationError):  # extra="forbid": no off switch exists
            type(_shadow_config()).model_validate(data)
        del data["llm"][key]
