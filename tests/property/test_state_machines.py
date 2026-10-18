"""WP-001: proposal and case-lifecycle state machines as properties (01 §7.10/§7.11).

The machines are data — a mapping of (current, event) → next — and
``next_status`` is a pure function. The property under test: walking random
event sequences never reaches an undefined state; every legal step lands on
its documented target and every illegal step raises a DomainValidationError
naming the current status and the event.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.lifecycle import TRANSITIONS as LIFECYCLE
from supportagent.domain.lifecycle import CaseEvent, CaseStatus
from supportagent.domain.lifecycle import next_status as lifecycle_next
from supportagent.domain.proposal_state import (
    TRANSITIONS as PROPOSALS,
)
from supportagent.domain.proposal_state import (
    ProposalEvent,
    ProposalStatus,
)
from supportagent.domain.proposal_state import next_status as proposal_next

PROPOSAL_EVENT = st.sampled_from(list(ProposalEvent))
CASE_EVENT = st.sampled_from(list(CaseEvent))


@given(events=st.lists(PROPOSAL_EVENT, max_size=12))
@settings(max_examples=200)
def test_proposal_transitions_legal(events: list[ProposalEvent]) -> None:
    current = ProposalStatus.PROPOSED
    for event in events:
        if (current, event) in PROPOSALS:
            assert proposal_next(current, event) is PROPOSALS[(current, event)]
        else:
            with pytest.raises(DomainValidationError):
                proposal_next(current, event)


@given(events=st.lists(PROPOSAL_EVENT, max_size=12))
@settings(max_examples=200)
def test_proposal_transitions_illegal_raise(events: list[ProposalEvent]) -> None:
    current = ProposalStatus.PROPOSED
    for event in events:
        if (current, event) not in PROPOSALS:
            with pytest.raises(DomainValidationError) as excinfo:
                proposal_next(current, event)
            message = str(excinfo.value)
            assert current.value in message
            assert event.value in message
        else:
            current = proposal_next(current, event)
    assert current in ProposalStatus  # never an undefined state


@given(events=st.lists(CASE_EVENT, max_size=12))
@settings(max_examples=200)
def test_lifecycle_transitions_legal(events: list[CaseEvent]) -> None:
    current = CaseStatus.OPEN
    for event in events:
        if (current, event) in LIFECYCLE:
            assert lifecycle_next(current, event) is LIFECYCLE[(current, event)]
        else:
            with pytest.raises(DomainValidationError):
                lifecycle_next(current, event)


@given(events=st.lists(CASE_EVENT, max_size=12))
@settings(max_examples=200)
def test_lifecycle_transitions_illegal_raise(events: list[CaseEvent]) -> None:
    current = CaseStatus.OPEN
    for event in events:
        if (current, event) not in LIFECYCLE:
            with pytest.raises(DomainValidationError) as excinfo:
                lifecycle_next(current, event)
            message = str(excinfo.value)
            assert current.value in message
            assert event.value in message
        else:
            current = lifecycle_next(current, event)
    assert current in CaseStatus  # never an undefined state
