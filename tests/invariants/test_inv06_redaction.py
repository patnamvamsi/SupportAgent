"""INV-06: no secret ever leaves the process boundary (01 §7.13).

Frozen test IDs (10-wp WP-007, INV-06):

- ``test_llm_bound_canaries_absent``
- ``test_redaction_counts_event_contains_no_values``

The LLM boundary is where a ticket's text becomes a prompt; INV-06 pins that
(1) no canary secret survives into any captured ``ProviderRequest`` (system
and user) nor into any event payload, and (2) the ``redact`` event carries
per-class *counts* only — never a value.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from supportagent.domain.detectors import DetectorClass
from supportagent.domain.events import EventKind
from supportagent.domain.model.triage import TriageResult
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext

# One trace per test (01 §4): the LLM call runs in its own span.
_TRACE = TraceContext.new("acme", "trace_0001", "span_llm")


def _plant(cls: DetectorClass, token: str) -> str:
    """Plant a canary in the shape its detector expects (01 §7.13)."""
    if cls is DetectorClass.GENERIC_API_KEY:
        return f"api_key={token}"
    if cls is DetectorClass.BEARER_TOKEN:
        return f"Authorization: Bearer {token}"
    return token


def _build_ticket(canaries: dict[DetectorClass, str], placement: str = "body") -> tuple[str, str]:
    """A ticket carrying every canary once, in the ``placement`` field (10-wp:
    canaries go in the ticket body **and** the subject)."""
    planted = "\n".join(f"- {_plant(cls, token)}" for cls, token in canaries.items())
    if placement == "subject":
        return f"Urgent outage {planted}", "Users report errors."
    return "Urgent outage", planted


def _key_material(token: str) -> list[str]:
    """The parts of a canary that must not leak: for a PEM block, every line
    (the key material is the secret, not just the header)."""
    return [line for line in token.splitlines() if line.strip()] or [token]


@pytest.mark.parametrize("placement", ["body", "subject"])
def test_llm_bound_canaries_absent(
    structured_llm: Any,
    fake_llm: Any,
    events: Any,
    canaries: dict[DetectorClass, str],
    placement: str,
) -> None:
    """INV-06: a canary token never reaches the provider or an event payload."""
    subject, body = _build_ticket(canaries, placement)
    structured_llm.structured(
        SlotName.CLASSIFY,
        {
            "taxonomy_labels": "- database/saturation: pool saturation",
            "pack_guidance": "",
            "ticket_subject": subject,
            "ticket_body": body,
        },
        TriageResult,
        trace=_TRACE,
    )

    # The pipeline reached the provider and recorded both events (01 §4, §7.13).
    assert fake_llm.captured, "pipeline never reached the provider"
    kinds = {event.kind for event in events.appended}
    assert EventKind.REDACT in kinds, "no redact event recorded"
    assert EventKind.LLM in kinds, "no llm event recorded"

    # (1) No secret survives into the provider request (system + user).
    captured = "\n".join(req.system + "\n" + req.user for req in fake_llm.captured)
    for cls, token in canaries.items():
        for part in _key_material(token):
            assert part not in captured, f"{cls.value} canary leaked into provider request"

    # (2) No secret survives into any event payload.
    for event in events.appended:
        blob = json.dumps(event.payload, default=str)
        for cls, token in canaries.items():
            assert token not in blob, (
                f"{cls.value} canary leaked into {event.kind.value} event payload"
            )


def test_redaction_counts_event_contains_no_values(
    structured_llm: Any, events: Any, canaries: dict[DetectorClass, str]
) -> None:
    """INV-06: the ``redact`` event carries counts_by_class only — never values."""
    subject, body = _build_ticket(canaries)
    structured_llm.structured(
        SlotName.CLASSIFY,
        {
            "taxonomy_labels": "- database/saturation: pool saturation",
            "pack_guidance": "",
            "ticket_subject": subject,
            "ticket_body": body,
        },
        TriageResult,
        trace=_TRACE,
    )

    redact_events = [event for event in events.appended if event.kind == EventKind.REDACT]
    assert redact_events, "no redact event emitted"
    payload = redact_events[0].payload
    assert payload["v"] == 1

    # Every planted class is counted (at least once).
    counts = payload["counts_by_class"]
    for cls in canaries:
        assert int(counts.get(cls.value, 0)) >= 1, f"missing redaction count for {cls.value}"

    # And NO secret value appears anywhere in the payload (counts only).
    blob = json.dumps(payload, default=str)
    for token in canaries.values():
        assert token not in blob, "redact event leaked a secret value"
