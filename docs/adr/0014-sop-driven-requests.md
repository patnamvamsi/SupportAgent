# 0014. SOP-driven requests: SOPs propose existing action kinds; auto mode is a conditioned grant

- **Status:** accepted (part 1); proposed (part 2, needs its own WP and review before code)
- **Date:** 2026-10-28
- **Code PR:** part 1 lands with WP-M08's last part (SOP-driven requests)
- **Spec sections changed:** none yet; part 2 will change 01 §6 (gate) and 03 (audit)

## Context

WP-M08 ends with SOP-driven requests: a team's written procedure (an SOP in the knowledge layer) drives what the agent asks to do. Two shapes were on the table:

- **(a)** SOP steps only *propose* action kinds that already exist, and each proposal goes through the gate as today.
- **(b)** A reviewed SOP becomes a runbook-like file that names action kinds.

The maintainer chose (a), and added a direction: later, depending on confidence, when certain checks are positive, or when a human puts certain SOPs on auto mode, actions should run without a per-action human gate. Every request served must be documented in detail, traceable and revertible.

What exists already:

- **Setup grants** (ADR-0005) let a human pre-approve a T1 kind for a service (`service:<name>:<kind>`). A grant-covered proposal is enqueued without a per-action approval.
- **INV-01:** every send traces to a human approval or a customer-approved grant. The DB trigger enforces it. Customer-facing kinds are never grant-authorized.
- **Assisted mode** (ADR-0008): promotion between modes is always a human decision.

## Decision

### Part 1 (accepted): SOPs propose, the gate decides

- An SOP is a knowledge doc (Git or Confluence) that names, per step, an **existing action kind** and its arguments as literals or case placeholders. No new kinds, no free text executed.
- RCA or triage may cite an SOP step. The step becomes a normal **proposal**: same kind, same validation, same tier, same gate, same approval or grant path. The SOP is recorded on the proposal as provenance (doc id, version, content hash, step).
- An SOP never raises a step's tier, never satisfies a check on its own (docs are guidance, ADR-0011 §4), and never names a target outside the case's service (the catalog check is unchanged).
- Eval-gated like every LLM feature: lab scenarios with SOP fixtures, including an SOP that names a wrong or unsafe step (it must still be gated) and a stale SOP.

### Part 2 (proposed): auto mode as a conditioned SOP grant

Auto mode is not a bypass of the gate. It is a **grant**, so INV-01 still holds: the trace leads to a human who approved the grant.

- **Opt-in per SOP, by a human.** A grant with scope `sop:<doc id>@<version>:<step>:<kind>`. A new SOP version, or an edited step, needs a new grant: approving one text never approves another.
- **Conditions on the grant**, all of which must hold at enqueue time:
  - named checks returned positive (tool results, not doc text);
  - a minimum confidence (the calibrated score, ADR-0002; thresholds from ADR-0003);
  - the service, and optionally a time window or rate cap.
  A condition that can't be evaluated fails closed, back to a human approval.
- **Revertible by construction.** Only kinds that declare a **revert** (an inverse action, or "append-only and harmless" like an internal work note) can be auto-granted. The revert is recorded with the action and offered in the UI.
- **Documented and traceable.** Each auto action records:
  - the grant;
  - the SOP version and step;
  - the checks and their results;
  - the score;
  - the request sent and the external ref (outbox and INV-04 already keep the last two).

  `/ui` shows them as one timeline per case.
- **Unchanged:**
  - Customer-facing kinds stay never grant-authorized, unless a later ADR changes that explicitly.
  - Promotion between modes stays a human decision.
  - Revoking the grant stops auto mode at once.

## Consequences

- Part 1 needs no change to the trust core. It adds provenance to proposals and a lab suite.
- Part 2 is a trust-core change: grant conditions, a revert declaration per action kind, and the audit timeline. It gets its own WP, with INV-01 and fault tests on both DB engines, before any code.
- Open for part 2: which existing kinds declare a safe revert; whether a confidence condition is allowed before ADR-0003's thresholds are calibrated on partner data (proposed: no).
