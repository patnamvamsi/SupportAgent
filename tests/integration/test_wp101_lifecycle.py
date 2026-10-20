"""WP-101 through the worker: ServiceNow changes drive the case lifecycle.

``bootstrap.build_worker`` polls FakeServiceNowHTTP through the real
``ServiceNowSource`` and ``SyncService``. ``ShadowInbound.on_changes`` hands
each known record's human changes to ``LifecycleService``.

Canonical test IDs from 11-wp §WP-101 that live here:
``test_sync_human_change_behind_echo_detected``,
``test_sync_automation_assign_not_takeover``,
``test_hand_back_by_reassign_to_integration_user``,
``test_new_alert_while_human_owned_is_duplicate_not_hand_back``,
``test_resolved_then_reopened_lifecycle``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP, SyncASGITransport
from supportagent.bootstrap import build_worker
from supportagent.entrypoints.worker.loop import Worker
from tests.integration.test_shadow_worker import _config

_SVC = "svc_supportagent"


@pytest.fixture
def sn() -> FakeServiceNowHTTP:
    fake = FakeServiceNowHTTP()
    fake.seed_row("sys_user", {"sys_id": "u-svc", "user_name": _SVC})
    return fake


@pytest.fixture
def worker(  # noqa: PLR0913, PLR0917
    tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes, sn: FakeServiceNowHTTP
) -> Worker:
    return build_worker(
        _config(tmp_path),
        master_key=master_key,
        clock=clock,
        ids=ids,
        transport=SyncASGITransport(sn.app),
        llm_provider=FakeLLMProvider(),
        engine=db,
    )


class _Ticket:
    """One incident on the fake instance; each ``change`` bumps ``sys_mod_count``."""

    def __init__(
        self,
        sn: FakeServiceNowHTTP,
        clock: Any,
        sys_id: str = "sys-1",
        number: str = "INC0010001",
    ) -> None:
        self.sn, self.clock, self.sys_id, self.number, self.mod = sn, clock, sys_id, number, 1
        self._put("alice")

    def _put(self, updated_by: str) -> None:
        self.sn.seed_record(
            "incident",
            self.sys_id,
            number=self.number,
            short_description="payroll-api 500s",
            priority="3",
            cmdb_ci="CI3003",
            caller_id="u-caller",
            sys_updated_on=self.clock.now(),
            sys_updated_by=updated_by,
            sys_mod_count=self.mod,
        )

    def change(self, user: str, field: str, new: str) -> None:
        self.clock.advance(60)
        self.mod += 1
        self.sn.seed_audit(
            "incident",
            self.sys_id,
            user=user,
            ts=self.clock.now(),
            fieldname=field,
            newvalue=new,
            checkpoint=self.mod,
        )
        self._put(user)

    def echo_note(self) -> None:
        """Our own work note after a human change (the last writer is the agent)."""
        self.clock.advance(1)
        self.mod += 1
        self.sn.seed_journal(
            "incident",
            self.sys_id,
            user=_SVC,
            ts=self.clock.now(),
            element="work_notes",
            value="note [sa:k]",
        )
        self._put(_SVC)


def _status(db: Engine, sys_id: str = "sys-1") -> str:
    with db.connect() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM cases WHERE external_id = :s ORDER BY opened_ts DESC"),
                {"s": sys_id},
            )
            .scalars()
            .first()
        )


def _events(db: Engine, kind: str) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM events WHERE kind = :k"), {"k": kind}
            ).scalar_one()
        )


def test_sync_human_change_behind_echo_detected(
    worker: Worker, db: Engine, clock: Any, sn: FakeServiceNowHTTP
) -> None:
    ticket = _Ticket(sn, clock)
    worker.run_once()
    before = _status(db)
    ticket.change("bob", "assigned_to", "u-bob")
    ticket.echo_note()  # the record's last writer is now us
    worker.run_once()
    assert before != "human_owned"
    assert _status(db) == "human_owned", "the human change behind our echo is still seen"
    assert _events(db, "sync_takeover") == 1


def test_sync_automation_assign_not_takeover(
    worker: Worker, db: Engine, clock: Any, sn: FakeServiceNowHTTP
) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, name, is_active, created_ts) "
                "VALUES ('sub-ada', 'acme', 'ada@x', 'Ada', 1, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO automation_actors (tenant_id, sys_user, kind, "
                "confirmed_by, confirmed_ts) VALUES ('acme', 'flow.designer', "
                "'integration', 'sub-ada', 1)"
            )
        )
    ticket = _Ticket(sn, clock)
    worker.run_once()
    before = _status(db)
    ticket.change("flow.designer", "assigned_to", "u-team")
    worker.run_once()
    assert _status(db) == before, "a confirmed automation actor's assignment is not takeover"
    assert _events(db, "sync_takeover") == 0


def test_hand_back_by_reassign_to_integration_user(
    worker: Worker, db: Engine, clock: Any, sn: FakeServiceNowHTTP
) -> None:
    ticket = _Ticket(sn, clock)
    worker.run_once()
    ticket.change("bob", "assigned_to", "u-bob")
    worker.run_once()
    assert _status(db) == "human_owned"
    ticket.change("bob", "assigned_to", "u-svc")  # re-assigned to the agent's sys_id
    worker.run_once()
    assert _status(db) == "in_progress"


def test_new_alert_while_human_owned_is_duplicate_not_hand_back(
    worker: Worker, db: Engine, clock: Any, sn: FakeServiceNowHTTP
) -> None:
    ticket = _Ticket(sn, clock)
    worker.run_once()
    ticket.change("bob", "state", "2")
    worker.run_once()
    assert _status(db) == "human_owned"
    # The same ticket identity arrives again as a new record: a duplicate.
    clock.advance(60)
    _Ticket(sn, clock, sys_id="sys-1b", number="INC0010001")
    worker.run_once()
    with db.connect() as conn:
        rows = conn.execute(text("SELECT status, duplicate_count FROM cases")).all()
    assert [tuple(r) for r in rows] == [("human_owned", 1)], "linked; still human-owned"


def test_resolved_then_reopened_lifecycle(
    worker: Worker, db: Engine, clock: Any, sn: FakeServiceNowHTTP
) -> None:
    ticket = _Ticket(sn, clock)
    worker.run_once()
    ticket.change("bob", "state", "6")
    worker.run_once()
    assert _status(db) == "resolved"
    with db.connect() as conn:
        assert conn.execute(text("SELECT resolved_ts FROM cases")).scalar_one() is not None
    ticket.change("carol", "state", "2")
    worker.run_once()
    assert _status(db) == "reopened"
    with db.connect() as conn:
        assert conn.execute(text("SELECT resolved_ts FROM cases")).scalar_one() is None
