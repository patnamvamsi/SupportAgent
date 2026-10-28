# 0014. SOP-driven requests: SOPs propose existing action kinds; auto mode is a conditioned grant

- **Status:** accepted (part 1); proposed (part 2, needs its own WP and review before code)
- **Date:** 2026-10-28
- **Code PR:** part 1: WP-M08 SOP-driven requests
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

- An SOP is a Git knowledge doc whose front matter has an `sop:` block: `when` (the hypotheses it applies to) and `steps` (`id`, `kind`, a short `note`). Steps use **existing internal note kinds only** (`work_note`, `enrich_page`); anything else, including a new kind, is refused at load. Field updates, reruns and customer-facing kinds wait for part 2's revert declarations. No body text is executed or becomes a payload.
- A step applies when RCA's outcome is SUPPORTED for a hypothesis in `when`. The `rca.run` job then submits it as a normal **proposal** through the gate, in the artifact's UoW. Shadow mode records the steps in the artifact only.
- The text is a fixed template around the step's reviewed, redacted note. The proposal records its provenance: doc, version, SOP hash, step, and the RCA artifact id.
- **The scope is the SOP's own:** `sop:<doc>@<hash>:<step>:<kind>`, where the hash covers the SOP block. No setup grant covers that scope, so in part 1 every SOP step waits for a human approval, even where a service-wide `work_note` grant exists. An edited SOP gets a new scope.
- An SOP never satisfies a check on its own (docs are guidance, ADR-0011 §4). A stale or out-of-scope SOP proposes nothing. The target is always the case's own record (the catalog check is unchanged).
- Eval-gated: the lab's `truth.sop_steps` names exactly the steps to propose. The scenarios cover the applicable SOP, an SOP for the wrong cause with an injected instruction, and a stale SOP.

### Part 2 (proposed): auto mode as a conditioned SOP grant

Auto mode is not a bypass of the gate. It is a **grant**, so INV-01 still holds: the trace leads to a human who approved the grant.

- **Opt-in per SOP, by a human.** A grant on part 1's scope `sop:<doc>@<hash>:<step>:<kind>`. An edited SOP has a new hash and needs a new grant: approving one text never approves another.
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

- Part 1 needs no change to the trust core: new proposals with their own scope, provenance in the payload, and a lab check.
- Part 2 is a trust-core change: grant conditions, a revert declaration per action kind, and the audit timeline. It gets its own WP, with INV-01 and fault tests on both DB engines, before any code.
- Open for part 2: which existing kinds declare a safe revert; whether a confidence condition is allowed before ADR-0003's thresholds are calibrated on partner data (proposed: no).
