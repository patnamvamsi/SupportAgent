# 0007. A storm group's non-lead members safe-default as `storm-member`

- **Status:** accepted
- **Date:** 2026-10-21
- **Code PR:** WP-111 (storm grouping and spend breaker)
- **Spec sections changed:** 01-architecture-and-contracts.md §4 (`TriageDecision.reason` gains one value); ARCHITECTURE.md §4.3 is implemented as written

## Context

ARCH §4.3: in storm mode, members get "**one** triage of the group … Other members inherit the group's route as a *suggestion* and are safe-defaulted to the human queue under the group". 11-wp WP-111 repeats it ("a non-lead member is linked and safe-defaulted with the group's route as its suggestion"). The closed reason set in 01 §4 has no value for "decided by the group, not on its own". `llm-failure` or `low-calibrated-score` would misreport why the LLM wasn't asked, and would distort the stats.

## Decision

A case that joins an open group while storm mode is on is decided **without** the pre-classifier or the LLM, as `safe_default` with reason **`storm-member`**. Its `classification`, `severity` and `llm_suggestion` carry the group lead's labels, with the summary naming the lead case, so the human sees the group's likely route. The lead itself (the case that opened the group) is triaged normally.

```python
reason: Literal["llm-failure", "classifier-conflict", "low-calibrated-score",
                "unconfirmed-high-severity", "unresolved-service",
                "no-paging-integration", "storm-member",
                "spend-breaker", "routed"]
```

## Consequences

- A storm costs one LLM classification per group, not per alert, which is what the storm soak bounds (WP-114).
- `stats` counts `storm-member` among the reasons decided before the pre-classifier, so abstain and conflict rates aren't distorted.
- A member joining a group *after* storm mode has ended is triaged normally: grouping links it, and the short-circuit applies only during the storm.
