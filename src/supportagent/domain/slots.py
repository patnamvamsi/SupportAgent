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


#: The variables each slot's caller supplies (BL-009). ``PromptRegistry.check``
#: compares them with every prompt's template at load, so a renamed or missing
#: variable is a ``ConfigError`` at startup rather than a failed call.
SLOT_VARIABLES: dict[SlotName, frozenset[str]] = {
    SlotName.CLASSIFY: frozenset({"taxonomy_labels", "ticket_subject", "ticket_body"}),
    SlotName.PROPOSE_HYPOTHESES: frozenset(
        {"hypothesis_ids", "max_hypotheses", "service", "classification", "severity"}
    ),
    SlotName.ENTAIL: frozenset({"claim", "evidence"}),
}
