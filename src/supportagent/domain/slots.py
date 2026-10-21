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
