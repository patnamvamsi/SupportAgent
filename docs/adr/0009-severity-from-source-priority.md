# 0009. Severity comes from the source priority

- **Status:** accepted
- **Date:** 2026-10-25
- **Code PR:** BL-044 (severity strategy)
- **Spec sections changed:** ARCHITECTURE.md §4 (where `severity` comes from), §4.2 (the severity cap); 02-interfaces.md §1 (`severity.strategy`), §6 (priority 5)

## Context

ARCH §4 had the LLM fill the `severity` slot, and routing used it. The first live smoke run on `qwen/qwen3.8-27b` (report `20261024T234356Z-smoke-live`) matched the label on 22 of 24 cases but graded severity one level too high on most of them (BL-044). The model sees only subject and body. The ServiceNow priority, which a person or the customer's own impact × urgency matrix already set, was mapped to `severity_hint` and stored on the case, but routing ignored it. So the case row and its route could disagree. The product owner decided that severity comes from ServiceNow priority, and that a separate severity calculation may replace it later.

## Decision

- A case's severity is decided by a **severity strategy** (`domain.severity.SeverityStrategy`), chosen by config `severity.strategy`.
- v0.1 ships one strategy, **`source_priority`** (the default): the source record's priority (ServiceNow `priority` 1-4 → P1-P4, 5 Planning → P4; a webhook's `severity_hint`). Only a record with no usable priority falls back to the LLM's severity.
- Each decision records `severity_origin`: `source_priority` or `llm`.
- **The ARCH §4.2 severity cap is unchanged.** A P1/P2 case without an agreeing pre-classifier label safe-defaults as `unconfirmed-high-severity`, whatever the severity's origin. The cap guards an uncross-checked *label* on the costliest cases, and a trusted severity doesn't make the label trustworthy.
- A future severity calculator (for example one combining priority, blast radius and alert signals) lands as a new `severity.strategy` value. The classifier doesn't change.

## Consequences

- Routing and the case row now agree on severity, and the LLM's severity error no longer moves a case's pathway when the source has a priority.
- More P1/P2 tickets reach the cap: a real ServiceNow P1 without a pre-classifier label now goes to a human with the LLM's suggestion attached. Watch the `unconfirmed-high-severity` rate in the ADR-0003 shadow import.
- The LLM still returns `severity`. It is kept in `llm_suggestion` and used for records without a priority.
- Tests: `test_decide_llm_only_p1_capped_with_suggestion` now runs without a hint (the LLM fallback). New: `test_decide_routes_on_the_source_priority_not_the_llm`, `test_decide_source_p1_without_preclass_is_still_capped`, `test_decide_uses_an_injected_severity_strategy`, `tests/unit/test_severity_source.py`. The WP-014 fixture gives group E ServiceNow priority 1; its golden report is unchanged. The test builder's default `severity_hint` is now P3.
