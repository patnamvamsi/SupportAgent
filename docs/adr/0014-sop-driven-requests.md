# 0014. SOP-driven requests: SOPs propose existing action kinds; auto mode is a conditioned grant

- **Status:** accepted (part 1, 2026-10-28; part 2, 2026-10-29 after the maintainer's review)
- **Date:** 2026-10-28
- **Code PR:** part 1: WP-M08 SOP-driven requests; part 2: BL-054 SOP auto mode
- **Spec sections changed:** part 2: 01 §7.10 (conditioned grants), 02 §2/§4 (`sop list`, `sop auto`, `POST /api/v1/proposals/{id}/retract`), 03 §2.3/§4 (`policy_grants.conditions_json`, migration 0015)

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

### Part 2 (accepted): auto mode as a conditioned SOP grant

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

### Part 2 as built (BL-054)

- **Turning it on.** `supportagent sop list` prints each SOP step's scope. `supportagent sop auto <scope> --check <hypothesis>… --max-per-day N [--service S…]` writes a `requested` grant whose conditions are stored with it (`policy_grants.conditions_json`). It refuses a step that isn't in a current, fresh SOP. An admin approves the grant in `/ui/grants`, which shows the conditions. To change the conditions, revoke the grant and request a new one.
- **Conditions.** `checks` (hypotheses whose tool checks ran and held in this RCA), `max_per_day` (1–50, counted over the last 24 hours on the scope), `services` (optional). Two rules apply whatever the conditions say:
  - the supported cause's claim passed `entail`;
  - the kind declares a revert.

  A time window is not built; the daily cap is the only limit on rate.
- **Fail closed.** These all send the proposal to a human as usual (`pending_approval`):
  - an `sop:` grant without conditions;
  - unreadable JSON or an unknown key;
  - a `min_score`;
  - missing evidence;
  - any condition that doesn't hold.

  A gate event `auto_declined` records each reason.
- **Revert declarations** (`domain.auto.REVERTS`):
  - `work_note` and `enrich_page` declare `retraction_note`: a fixed-template note of the same kind on the same record, withdrawing the original.
  - No other kind declares a revert, so no other kind can run in auto mode.
  - A retraction is proposed from the case page or `POST /api/v1/proposals/{id}/retract`. It has its own scope `retract:<proposal>:<kind>`, which no grant covers, so a person approves it. One live retraction per action.
- **Confidence.** No confidence condition until ADR-0003's thresholds are calibrated on partner data. RCA's `score` is a reporting share, not a calibrated probability. A stored `min_score` fails closed.
- **The record.** Every evaluation writes a `gate` event on the case's trace with:
  - the grant;
  - its conditions;
  - the evidence (positive checks, `entail` result, RCA artifact id);
  - the day's count;
  - any refusals.

  The proposal payload already carries the SOP doc, hash, step, artifact id and revert. `/ui/cases/{id}` shows one row per evaluation: the decision, the grant and conditions, the evidence, the send (outbox status, external ref), the revert, and a Retract button.
- **The page note** (BL-040) counts steps waiting for approval and steps that ran on auto separately.

## Consequences

- Part 1 needs no change to the trust core: new proposals with their own scope, provenance in the payload, and a lab check.
- Part 2 is a trust-core change: grant conditions, a revert declaration per action kind, and the audit timeline. It gets its own WP, with INV-01 and fault tests on both DB engines, before any code.
- Part 2's open questions, decided: only the two internal note kinds declare a revert (a retraction note); a confidence condition waits for calibrated thresholds.
- Part 2 changes no DB trigger: INV-01 path 2 still checks an approved, in-scope, unexpired grant, and conditions only narrow what it covers. Auto sends go out on that grant, so their trace leads to the admin who approved it.
- Adding a kind to `REVERTS` (a field update with an inverse, say) is a trust-core change: it needs its own ADR amendment and fault tests.
