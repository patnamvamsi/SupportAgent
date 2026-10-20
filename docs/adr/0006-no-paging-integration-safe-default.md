# 0006. A service without a paging integration safe-defaults as `no-paging-integration`

- **Status:** accepted
- **Date:** 2026-10-20
- **Code PR:** WP-015 (doctor)
- **Spec sections changed:** 01-architecture-and-contracts.md §4 (`TriageDecision.reason` gains one value); ARCHITECTURE.md §14 is implemented as written

## Context

ARCH §14 makes an external paging integration a deployment prerequisite: "`doctor` checks that one is configured for every service in the catalog, and **refuses to start triage for a service without one**". 10-wp WP-015 tests this as `test_doctor_paging_missing_blocks_triage`. A doctor finding alone can't refuse anything at triage time, and refusing to start the whole worker would block every service because one catalog row is incomplete.

The decision reasons in 01 §4 are a closed `Literal`. None of them says "this service isn't eligible", and reusing `unresolved-service` would hide a configuration gap behind a resolution failure in the stats and on the case.

## Decision

`TriageService` checks the resolved service right after resolution, before the pre-classifier or the LLM runs. A service whose catalog row has no `paging_integration` gets the safe default **`no-paging-integration`**: the case and its decision are written, nothing is proposed, and the case goes to the human queue (INV-05). `doctor`'s `paging` check fails and names the services.

`TriageDecision.reason` (01 §4) becomes:

```python
reason: Literal["llm-failure", "classifier-conflict", "low-calibrated-score",
                "unconfirmed-high-severity", "unresolved-service", "no-paging-integration",
                "spend-breaker", "routed"]
```

## Consequences

- An incomplete catalog row stops triage for that service only, and the reason is visible on the case, in `stats` (safe defaults by reason) and in `doctor`.
- `stats` counts `no-paging-integration` among the reasons decided before the pre-classifier, so it doesn't distort the abstain and conflict rates.
- Shadow mode applies the same rule, so its counterfactual matches what assisted mode would do.
- The test catalogs now carry a `paging_integration`, as `catalog/services.example.yaml` already did.
