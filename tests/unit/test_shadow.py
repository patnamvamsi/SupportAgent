"""WP-014: ShadowRunner and shadow import (ARCH §8.4, CAP §4.7, 03 §2.6).

Canonical test IDs from 10-wp §WP-014:
``test_shadow_runner_writes_no_outbox_or_proposals``,
``test_shadow_import_replays_in_time_order``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from supportagent.adapters.db.repos.shadow import ShadowRepo
from supportagent.adapters.servicenow.export import read_export
from supportagent.app.shadow import ShadowRunner, import_history
from supportagent.domain.errors import ConfigError
from supportagent.domain.events import EventKind
from supportagent.domain.model.shadow import HistoricalIncident
from supportagent.domain.severity import Severity
from supportagent.domain.shadow import agreement
from tests.unit.test_triage_service import _evt, _llm_json, _seed_grant, _wire


class _Spy:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.kinds: list[EventKind] = []

    def append(self, event: Any) -> None:
        self.kinds.append(event.kind)
        self.inner.append(event)


def _runner(uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any) -> tuple[ShadowRunner, Any]:
    w = _wire(uow, clock, ids, aead, fake_llm)
    spy = _Spy(w.events)
    runner = ShadowRunner(
        uow=uow,
        clock=clock,
        ids=ids,
        events=spy,
        case_repo=w.case_repo,
        classifier=w.triage._classifier,
        catalog=w.catalog,
        repo=ShadowRepo(uow),
    )
    return runner, spy


def _count(db: Any, table: str) -> int:
    with db.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())  # noqa: S608


def test_shadow_runner_writes_no_outbox_or_proposals(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    # Even with a setup grant that would auto-approve it live, shadow proposes
    # nothing: no proposals, no approvals, no outbox, no gate event.
    fake_llm.respond(_llm_json())
    _seed_grant(db)
    runner, spy = _runner(uow, clock, ids, aead, fake_llm)
    record = runner.run(_evt())
    assert record is not None
    assert (_count(db, "proposals"), _count(db, "approvals"), _count(db, "outbox")) == (0, 0, 0)
    assert EventKind.GATE not in spy.kinds
    assert EventKind.DECISION in spy.kinds
    assert _count(db, "shadow_records") == 1
    # The counterfactual: what it would have proposed, and when.
    assert record.first_proposal_at == clock.now()
    proposed: dict[str, Any] = dict(record.proposed)
    assert proposed["decision"]["outcome"] == "routed"
    specs = proposed["specs"]
    assert [s["kind"] for s in specs] == ["work_note"]
    assert specs[0]["scope"] == "service:payments:work_note"
    with db.connect() as conn:
        status = conn.execute(text("SELECT status FROM cases")).scalar_one()
    assert status == "triaged", "the case is still triaged (INV-02 dedup needs the row)"


def test_shadow_safe_default_has_no_first_proposal(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    fake_llm.respond(_llm_json())
    runner, _ = _runner(uow, clock, ids, aead, fake_llm)
    record = runner.run(_evt(resource="unknown-service", cmdb_ci=None))
    assert record is not None
    assert record.first_proposal_at is None
    proposed: dict[str, Any] = dict(record.proposed)
    assert proposed["decision"]["reason"] == "unresolved-service"
    assert proposed["specs"] == []


def test_shadow_duplicate_keeps_one_record(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    fake_llm.respond(_llm_json())
    runner, _ = _runner(uow, clock, ids, aead, fake_llm)
    first = runner.run(_evt())
    clock.advance(60)
    second = runner.run(_evt())
    assert first is not None and second is not None
    assert second.id == first.id
    assert _count(db, "shadow_records") == 1
    assert _count(db, "cases") == 1


@pytest.mark.parametrize(
    ("proposed", "human", "expected"),
    [
        (("database/saturation", "P2"), ("database/saturation", "P2"), True),
        (("database/saturation", "P2"), ("database/leak", "P2"), True),  # same domain
        (("database/saturation", "P2"), ("network/latency", "P2"), False),
        (("database/saturation", "P2"), ("database/saturation", "P1"), False),  # exact severity
        ((None, None), ("database/saturation", "P2"), False),  # the agent did not triage
        (("database/saturation", "P2"), (None, None), None),  # no human label yet
    ],
)
def test_agreement_is_prefix_match_and_exact_severity(
    proposed: tuple[str | None, str | None],
    human: tuple[str | None, str | None],
    expected: bool | None,
) -> None:
    def sev(v: str | None) -> Severity | None:
        return Severity(v) if v else None

    assert agreement(proposed[0], sev(proposed[1]), human[0], sev(human[1])) is expected


def _line(number: str, opened: str, cls: str, sev: str, priority: str = "3") -> str:
    return json.dumps(
        {
            "record": {
                "sys_id": f"sys_{number}",
                "number": number,
                "short_description": f"payments api failure {number}",
                "description": "5xx from payments",
                "priority": priority,
                "cmdb_ci": "CIPAYMENTS",
                "opened_at": opened,
                "sys_updated_on": opened,
                "sys_updated_by": "someone",
                "sys_mod_count": "3",
            },
            "human": {"classification": cls, "severity": sev, "first_action_at": opened},
        }
    )


def test_read_export_parses_and_rejects_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "export.jsonl"
    path.write_text(
        _line("INC1", "2026-01-02 10:00:00", "payments/api", "P3") + "\n\n", encoding="utf-8"
    )
    [incident] = list(read_export(path, tenant_id="acme"))
    assert incident.evt.ticket_number == "INC1"
    assert incident.evt.origin == "ticket"
    assert incident.opened_at == datetime(2026, 1, 2, 10, tzinfo=UTC)
    assert incident.human_severity == Severity.P3
    path.write_text('{"record": {}}\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="line 1"):
        list(read_export(path, tenant_id="acme"))


def test_shadow_import_replays_in_time_order(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any, tmp_path: Path
) -> None:
    # 10-wp WP-014: records are replayed in opened_at order, with the clock set
    # to each record's opened_at, and the human labels decide agreement.
    fake_llm.respond(_llm_json())  # payments/api P3
    path = tmp_path / "export.jsonl"
    path.write_text(
        "\n".join(
            [
                _line("INC3", "2026-01-03 09:00:00", "payments/api", "P3"),
                _line("INC1", "2026-01-01 09:00:00", "payments/db", "P3"),  # sibling: agrees
                _line("INC2", "2026-01-02 09:00:00", "payments/api", "P2"),  # severity differs
            ]
        ),
        encoding="utf-8",
    )
    runner, _ = _runner(uow, clock, ids, aead, fake_llm)
    seen: list[datetime] = []

    class _Clock:
        def now(self) -> datetime:
            now: datetime = clock.now()
            return now

        def set(self, when: datetime) -> None:
            seen.append(when)
            clock.set(when)

    incidents = list(read_export(path, tenant_id="acme"))
    records = import_history(incidents, runner=runner, clock=_Clock())
    assert seen == sorted(seen), "replayed in opened_at order"
    by_number = {r.proposed["ticket_number"]: r for r in records}
    assert list(by_number) == ["INC1", "INC2", "INC3"]
    for number, day in (("INC1", 1), ("INC2", 2), ("INC3", 3)):
        rec = by_number[number]
        assert rec.alert_received_at == datetime(2026, 1, day, 9, tzinfo=UTC)
        assert rec.first_proposal_at == datetime(2026, 1, day, 9, tzinfo=UTC)
    assert [by_number[n].agreement for n in ("INC1", "INC2", "INC3")] == [True, False, True]
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT human_classification, agreement FROM shadow_records ORDER BY id")
        ).all()
    assert len(rows) == 3
    assert {r[1] for r in rows} == {0, 1}


def test_import_duplicate_ticket_keeps_one_record(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    # Two exports of the same ticket fingerprint to one case and one record.
    fake_llm.respond(_llm_json())
    runner, _ = _runner(uow, clock, ids, aead, fake_llm)
    when = datetime(2026, 1, 1, tzinfo=UTC)
    evt = _evt(origin="ticket", ticket_number="INC9", received_at=when)
    incident = HistoricalIncident(
        evt=evt,
        opened_at=when,
        human_classification="payments/api",
        human_severity=Severity.P3,
        human_first_action_at=when + timedelta(minutes=5),
    )

    records = import_history([incident, incident], runner=runner, clock=clock)
    assert len(records) == 2 and records[0].id == records[1].id
    assert _count(db, "shadow_records") == 1


def test_shadow_container_swaps_in_the_shadow_runner() -> None:
    # 10-wp WP-011/014: shadow mode builds the ShadowRunner, never a live TriageService
    # (which would submit proposals) and never a TicketSink.
    from supportagent.bootstrap import build_container  # noqa: PLC0415
    from tests.unit.test_triage_service import _shadow_config  # noqa: PLC0415

    container = build_container(_shadow_config())
    assert "ShadowRunner" in container and "StatsService" in container
    assert "TriageService" not in container
    assert "TicketSink" not in container


def test_shadow_inbound_change_without_a_case_gets_its_own_trace(
    uow: Any, clock: Any, ids: Any, aead: Any
) -> None:
    from supportagent.adapters.db.repos.cases import CaseRepo  # noqa: PLC0415
    from supportagent.adapters.db.repos.events import SqlEventSink  # noqa: PLC0415
    from supportagent.app.shadow import ShadowInbound  # noqa: PLC0415
    from supportagent.domain.model.external import ExternalCaseRef  # noqa: PLC0415
    from supportagent.domain.model.ticket import ChangeRecord  # noqa: PLC0415

    captured: list[Any] = []

    class _Capture:
        def append(self, event: Any) -> None:
            captured.append(event)
            SqlEventSink(uow=uow, cipher=aead).append(event)

    inbound = ShadowInbound(
        runner=None,  # type: ignore[arg-type]  # on_changes never reaches the runner
        uow=uow,
        cases=CaseRepo(uow),
        events=_Capture(),
        ids=ids,
        clock=clock,
        tenant_id="acme",
    )
    change = ChangeRecord(
        source="audit", user="bob", ts=clock.now(), fieldname="state", newvalue="6"
    )
    inbound.on_changes(ExternalCaseRef(table="incident", sys_id="unknown"), [change, change])
    (event,) = captured
    assert event.kind is EventKind.SYNC
    assert event.trace_id == f"tr_{event.id}"
    assert event.payload["case_id"] is None
    assert (event.payload["rows"], event.payload["fields"]) == (2, ["state"])
    assert "6" not in json.dumps(dict(event.payload)), "values stay in ServiceNow"
