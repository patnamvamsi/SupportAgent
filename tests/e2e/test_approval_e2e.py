"""WP-013 DoD: alert → proposal in the UI → bob approves → one ServiceNow note → replay.

Loop A runs on the real DB with the fake LLM and no setup grant, so the
work-note proposal waits in ``pending_approval``. The admin gives bob the
approver role through the API; bob approves on the proposal page with the hash
of the payload he was shown; the outbox executor posts the note to
FakeServiceNow once; the trace replay shows bob's approval and the send.
"""

from __future__ import annotations

import html
import re
from typing import Any

from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.domain.model.triage import Triaged
from supportagent.domain.proposal_state import ProposalStatus
from supportagent.ports.tickets import NoteVisibility, RecordType
from tests.integration.test_wp011_dod import (
    SYS_ID,
    _catalog,
    _evt,
    _executor,
    _llm_json,
    _wire,
)
from tests.unit.api_support import ADA, BOB, csrf, login, logout, make_client


def _field(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', page)
    assert match, f"form field {name!r} missing"
    return html.unescape(match.group(1))


def test_alert_to_approved_work_note_e2e(  # noqa: PLR0913, PLR0917
    db: Any,
    uow: Any,
    clock: Any,
    ids: Any,
    aead: Any,
    master_key: bytes,
    fake_llm: Any,
    fake_sn: Any,
) -> None:
    # 1. An alert is triaged and routed; with no grant the proposal needs a human.
    fake_llm.respond(_llm_json())
    wired = _wire(uow, clock, ids, aead, fake_llm)
    outcome = wired.triage.handle(_evt())
    assert isinstance(outcome, Triaged)
    proposal = outcome.proposals[0]
    assert proposal.status == ProposalStatus.PENDING_APPROVAL

    # 2. Ada (first login: admin) makes Bob an approver.
    oidc = FakeOIDC()
    client = make_client(db, clock, ids, master_key, oidc, cat=_catalog())
    login(client, oidc, ADA)
    logout(client)
    login(client, oidc, BOB)
    logout(client)
    login(client, oidc, ADA)
    granted = client.post(
        "/api/v1/users/sub-bob/roles", json={"role": "approver"}, headers=csrf(client)
    )
    assert granted.status_code == 200
    logout(client)

    # 3. Bob sees it in the queue and approves exactly the payload he is shown.
    login(client, oidc, BOB)
    assert proposal.id in client.get("/ui/approvals").text
    page = client.get(f"/ui/proposals/{proposal.id}").text
    approved = client.post(
        f"/ui/proposals/{proposal.id}/approve",
        data={
            "csrf_token": _field(page, "csrf_token"),
            "payload_hash": _field(page, "payload_hash"),
            "reason": "matches the alert",
        },
        follow_redirects=False,
    )
    assert approved.status_code == 303

    # 4. The executor posts the work note to ServiceNow exactly once.
    executor = _executor(uow, clock, ids, wired.events, fake_sn)
    result = executor.run_once(clock.now())
    assert result.status == "sent", result.error
    assert executor.run_once(clock.now()).status == "none"
    notes = fake_sn.journal_entries(RecordType.INCIDENT, SYS_ID, NoteVisibility.INTERNAL)
    assert len(notes) == 1
    assert fake_sn.write_count == 1

    # 5. Replay shows the whole path, including Bob's approval and the send.
    view = client.get(f"/api/v1/traces/{proposal.trace_id}").json()
    kinds = [e["kind"] for e in view["events"]]
    for expected in ("ingest", "dedup", "decision", "gate", "send"):
        assert expected in kinds, f"{expected} missing from {kinds}"
    approvals = [
        e for e in view["events"] if e["kind"] == "gate" and "transition=approved" in e["summary"]
    ]
    assert [e["actor"] for e in approvals] == ["sub-bob"]
