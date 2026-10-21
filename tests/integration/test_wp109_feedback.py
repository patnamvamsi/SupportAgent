"""WP-109 through the worker: resolve-time ``outcome`` feedback and shadow labels.

Canonical test IDs from 11-wp §WP-109 that live here:
``test_feedback_outcome_on_resolve``, ``test_shadow_record_human_label_filled_on_resolve``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP, SyncASGITransport
from supportagent.bootstrap import build_worker
from supportagent.entrypoints.worker.loop import Worker
from tests.integration.test_shadow_worker import _config
from tests.integration.test_wp101_lifecycle import _SVC, _Ticket


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


def _one(db: Engine, sql: str) -> Any:
    with db.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def _resolve(worker: Worker, sn: FakeServiceNowHTTP, clock: Any) -> None:
    ticket = _Ticket(sn, clock)
    worker.run_once()  # the case and its shadow record
    ticket.change("bob", "priority", "2")
    ticket.change("bob", "category", "Database")
    ticket.change("bob", "subcategory", "Saturation")
    ticket.change("bob", "state", "6")
    worker.run_once()


def test_feedback_outcome_on_resolve(
    worker: Worker, db: Engine, clock: Any, sn: FakeServiceNowHTTP
) -> None:
    _resolve(worker, sn, clock)
    assert _one(db, "SELECT status FROM cases") == "resolved"
    outcome = json.loads(_one(db, "SELECT payload FROM feedback WHERE kind = 'outcome'"))
    assert (outcome["classification"], outcome["severity"]) == ("database/saturation", "P2")
    assert outcome["first_human_action_at"]
    assert _one(db, "SELECT COUNT(*) FROM feedback WHERE kind = 'diff'") == 4


def test_shadow_record_human_label_filled_on_resolve(
    worker: Worker, db: Engine, clock: Any, sn: FakeServiceNowHTTP
) -> None:
    _resolve(worker, sn, clock)
    with db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT human_classification, human_severity, human_first_action_ts, agreement "
                "FROM shadow_records"
            )
        ).one()
    assert (row[0], row[1]) == ("database/saturation", "P2")
    assert row[2] is not None
    assert row[3] in (0, 1), "agreement is computed once the human label exists"


def test_outcome_uses_the_classification_map(  # noqa: PLR0913, PLR0917
    tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes, sn: FakeServiceNowHTTP
) -> None:
    """BL-035: the partner's categories reach shadow agreement as taxonomy labels."""
    config = _config(tmp_path)
    mapped = config.model_copy(
        update={
            "servicenow": config.servicenow.model_copy(
                update={"classification_map": {"db/pool": "database/saturation"}}
            )
        }
    )
    worker = build_worker(
        mapped,
        master_key=master_key,
        clock=clock,
        ids=ids,
        transport=SyncASGITransport(sn.app),
        llm_provider=FakeLLMProvider(),
        engine=db,
    )
    ticket = _Ticket(sn, clock)
    worker.run_once()
    ticket.change("bob", "priority", "2")
    ticket.change("bob", "category", "DB")
    ticket.change("bob", "subcategory", "Pool")
    ticket.change("bob", "state", "6")
    worker.run_once()
    outcome = json.loads(_one(db, "SELECT payload FROM feedback WHERE kind = 'outcome'"))
    assert outcome["classification"] == "database/saturation"
    assert outcome["servicenow_category"] == "DB/Pool"
    assert _one(db, "SELECT human_classification FROM shadow_records") == "database/saturation"
