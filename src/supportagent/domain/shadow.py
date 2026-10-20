"""Triage agreement for the shadow promotion gate (CAP §4.7, 10-wp WP-014)."""

from __future__ import annotations

from supportagent.domain.classification import prefix_match
from supportagent.domain.severity import Severity


def agreement(
    proposed_classification: str | None,
    proposed_severity: Severity | None,
    human_classification: str | None,
    human_severity: Severity | None,
) -> bool | None:
    """Agreement = classification ``prefix_match`` **and** exact severity (10-wp WP-014).

    ``None`` while the human label is unknown (the row is not counted). A case
    the agent did not triage (no classification or severity — e.g. an LLM
    failure or an unresolved service) is a disagreement: the promotion gate
    must not be passed by declining to answer.
    """
    if human_classification is None or human_severity is None:
        return None
    if proposed_classification is None or proposed_severity is None:
        return False
    return (
        prefix_match(proposed_classification, human_classification)
        and proposed_severity == human_severity
    )
