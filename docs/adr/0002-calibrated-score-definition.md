# 0002. `calibrated_score` is the Wilson lower bound of per-label holdout accuracy

- **Status:** accepted
- **Date:** 2026-10-17
- **Code PR:** WP-010 (consumer), WP-105 (producer)
- **Spec sections changed:** ARCHITECTURE.md §4.2 (precision added; behaviour unchanged)

## Context

ARCH §4.2 defines `calibrated_score` as the "empirical accuracy of the LLM's label on the eval holdout", fail-closed, with no entry scoring 0. A raw accuracy over a handful of cases (for example 3 out of 3) would pass a 0.8 floor on almost no evidence.

## Decision

`calibrated_score = wilson_lower_bound(correct, n)` for the label's `agree` or `abstain` row in `evals/artifacts/calibration.json`. It is 0.0 when the row is missing or `n < 10` (prov.). The file is produced only by an eval run: the smoke suite (WP-016, `grade: smoke`) or the quality suite (WP-105, `grade: holdout`). It records its `grade`, `snapshot_id` and `model_id`, and is rejected at load if its `model_id` differs from the configured classify model or its grade is below `calibration.min_grade` (`smoke` in v0.0, `holdout` from v0.1).

## Consequences

Labels need enough holdout evidence to route automatically. A model change invalidates calibration until the suite is re-run, which is consistent with the model-deprecation runbook (ARCH §9).
