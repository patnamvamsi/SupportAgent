# 0008. Assisted mode ships in v0.1

- **Status:** accepted
- **Date:** 2026-10-21
- **Code PR:** BL-034 (assisted-mode worker wiring)
- **Spec sections changed:** 02-interfaces.md §1 (`mode`), §7 (jobs per mode); CAPABILITIES.md §4.7 and the release map; ROADMAP.md Phase 10b keeps the treatment/control split

## Context

The spec put assisted mode in v0.2 (ROADMAP Phase 10b, CAP release map), and v0.0/v0.1 code refused any mode but `shadow`. Everything assisted mode needs was already built and tested: the gate, grant-covered enqueue (BL-024), the outbox executor with INV-04 reconciliation, the claim lease sized to the sink (BL-001), comms plans and cadence (WP-102), takeover (WP-101), `enrich_page` (WP-112), `proposals.expire` (BL-028). Only the worker wiring was missing. The product owner decided to ship assisted mode in v0.1.

## Decision

- `mode` accepts **`shadow` and `assisted`** in v0.1. `standard` is still refused before v0.2.
- In assisted mode the worker:
  - triages with the real `TriageService` (proposals go through the gate);
  - schedules `outbox.execute` (2 s, at most 50 rows per drain; the job lease equals the claim lease, BL-002) and `comms.cadence` (60 s), alongside the shadow jobs;
  - registers the `enrich_page` handler when a paging provider is configured.
- A `comms` row is marked `sent` or `failed` from its outbox outcome (ARCH §6.3 history).
- **Unchanged:**
  - **Promotion is a human decision.** The shadow promotion gate (CAP §4.7, `/ui/stats`) informs it; nothing switches modes automatically.
  - **Assisted mode refuses to start** until every automation-actor candidate is confirmed or rejected (WP-101).
  - **INV-01..06 are unchanged.** Every send traces to a human approval or a customer-approved grant, enforced by the DB trigger.
- **Deferred to v0.2:** the treatment/control split (ARCH §8.4, C22). In v0.1, assisted mode applies to every catalog service. The assisted-delta value number therefore still comes from v0.2.

## Consequences

- A partner can move from shadow to assisted within v0.1, once the promotion gate is passed and they decide to.
- The first live sends happen in v0.1. The storm soak (WP-114), the fault suite (WP-106) and the INV-01/INV-04 suites are the guard rails, and they already run on both engines.
- Tests: `test_config_assisted_mode_rejected_before_v02` became `test_config_assisted_mode_accepted_in_v01` (`test_config_standard_mode_rejected_before_v02` stays). New: `tests/integration/test_bl034_assisted.py`.
- Watch: approval fatigue (`/ui/stats`) and `outbox` failures in the first assisted week.
