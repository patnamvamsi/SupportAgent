"""BL-024: a grant-authorized proposal reaches the outbox in the same unit of work.

The approval path enqueues inside the approval's UoW (WP-013). The grant path
now does too: ``GateService.submit`` with an outbox writer inserts the row
under the grant's authorization and moves the proposal ``approved →
executed``, all or nothing.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from supportagent.adapters.db.repos.outbox import OutboxRepo
from supportagent.app.gate import GateService
from supportagent.app.outbox import OutboxWriter
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.proposal import Proposal
from supportagent.domain.proposal_state import ProposalStatus
from tests.invariants.test_inv01_gate import (
    CASE_ID,
    _catalog,
    _scalar,
    _seed_base,
    _seed_grant,
    _spec,
    _trace,
)


def _gate(uow: Any, clock: Any, ids: Any, events: Any, writer: Any) -> GateService:
    return GateService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        catalog=_catalog(),
        ttl=timedelta(hours=1),
        outbox=writer,
    )


def test_grant_authorized_proposal_is_enqueued_and_executed(
    db: Any, uow: Any, clock: Any, ids: Any, events: Any
) -> None:
    _seed_base(db)
    _seed_grant(db, id="grnt_1", kind="work_note", scope="scope:A")
    writer = OutboxWriter(uow=uow, repo=OutboxRepo(uow), ids=ids)
    proposal = _gate(uow, clock, ids, events, writer).submit(
        CASE_ID, _spec(ActionKind.WORK_NOTE, scope="scope:A"), _trace()
    )
    assert isinstance(proposal, Proposal)
    assert proposal.status is ProposalStatus.EXECUTED
    assert _scalar(db, "SELECT status FROM proposals WHERE id = :i", i=proposal.id) == "executed"
    assert _scalar(db, "SELECT COUNT(*) FROM outbox") == 1
    assert _scalar(db, "SELECT policy_grant_id FROM outbox") == "grnt_1"
    assert _scalar(db, "SELECT approval_id FROM outbox") is None


def test_ungranted_proposal_waits_for_approval_and_is_not_enqueued(
    db: Any, uow: Any, clock: Any, ids: Any, events: Any
) -> None:
    _seed_base(db)
    writer = OutboxWriter(uow=uow, repo=OutboxRepo(uow), ids=ids)
    proposal = _gate(uow, clock, ids, events, writer).submit(
        CASE_ID, _spec(ActionKind.WORK_NOTE, scope="scope:A"), _trace()
    )
    assert isinstance(proposal, Proposal)
    assert proposal.status is ProposalStatus.PENDING_APPROVAL
    assert _scalar(db, "SELECT COUNT(*) FROM outbox") == 0


def test_enqueue_failure_rolls_back_the_proposal(
    db: Any, uow: Any, clock: Any, ids: Any, events: Any
) -> None:
    _seed_base(db)
    _seed_grant(db, id="grnt_1", kind="work_note", scope="scope:A")

    class _Broken:
        def enqueue(self, *args: Any) -> Any:
            raise RuntimeError("outbox insert failed")  # noqa: TRY003

    with pytest.raises(RuntimeError, match="outbox insert failed"):
        _gate(uow, clock, ids, events, _Broken()).submit(
            CASE_ID, _spec(ActionKind.WORK_NOTE, scope="scope:A"), _trace()
        )
    assert _scalar(db, "SELECT COUNT(*) FROM proposals") == 0, "all or nothing"
    assert _scalar(db, "SELECT COUNT(*) FROM outbox") == 0
