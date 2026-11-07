"""Named LLM slots (02 §5): a slot is one reusable prompt + model binding.

A *slot* is a named, versioned prompt (``prompts/<slot>/v<N>.md``) paired with
a model/timeout/token budget. Application code calls ``structured(slot, ...)``
and never touches a raw model id or prompt template (02 §5).
"""

from __future__ import annotations

from enum import StrEnum


class SlotName(StrEnum):
    """The closed set of LLM slots the system knows (02 §5)."""

    CLASSIFY = "classify"
    #: v0.1 (WP-110): RCA hypothesis proposal and claim entailment (ARCH §5, §5.4).
    PROPOSE_HYPOTHESES = "propose_hypotheses"
    ENTAIL = "entail"
    #: ADR-0015 (WP-O3): the facts a documentation page states about the system.
    EXTRACT_GRAPH = "extract_graph"
    #: ADR-0015 (WP-O4): a batch of past incidents mapped to labels and causes.
    MAP_INCIDENTS = "map_incidents"
    #: ADR-0021 §5: a traced object's answer over numbered facts, and the check
    #: that keeps a sentence only when the facts it cites support it.
    EXPLAIN_TRACE = "explain_trace"
    CHECK_TRACE = "check_trace"


#: The variables each slot's caller supplies (BL-009). ``PromptRegistry.check``
#: compares them with every prompt's template at load, so a renamed or missing
#: variable is a ``ConfigError`` at startup rather than a failed call.
SLOT_VARIABLES: dict[SlotName, frozenset[str]] = {
    SlotName.CLASSIFY: frozenset(
        {"taxonomy_labels", "pack_guidance", "ticket_subject", "ticket_body"}
    ),
    SlotName.PROPOSE_HYPOTHESES: frozenset(
        {
            "hypothesis_ids",
            "max_hypotheses",
            "service",
            "classification",
            "severity",
            "incident",
            "guidance",
        }
    ),
    SlotName.ENTAIL: frozenset({"claim", "evidence"}),
    SlotName.EXTRACT_GRAPH: frozenset({"vocabulary", "known_entities", "page_title", "page_text"}),
    SlotName.MAP_INCIDENTS: frozenset({"labels", "causes", "incidents"}),
    SlotName.EXPLAIN_TRACE: frozenset({"object", "object_id", "flow", "facts"}),
    SlotName.CHECK_TRACE: frozenset({"sentence", "facts"}),
}
