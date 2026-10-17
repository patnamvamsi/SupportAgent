# SupportAgent: Critical Review, Round 3

**Scope:** the current `ARCHITECTURE.md` (793 lines), `CAPABILITIES.md`, `ROADMAP.md`, and the new `CHANGELOG.md` and `PITCH.md`, reviewed against [critique-v2.md](critique-v2.md). There is still no code.
**Stance:** adversarial. This round focuses on three questions: (1) does the new SQL/pseudo-code do what the prose says, (2) does the CHANGELOG's "accepted, landed at X" match what X actually says, and (3) what can only be learned by building it?

---

## 0. Verdict

This is the strongest revision yet. INV-01 is now a real trigger, redaction lives in the client, dedup is a schema property, the RCA plan is a single algorithm with a truth table, the hypothesis taxonomy and service catalog exist, the paging split is right, and the review prose moved out of the spec. Round 2's major findings were taken seriously.

Three problems remain, and the third matters most:

1. **The new DDL has holes of its own.** The INV-01 trigger checks only INSERT, never UPDATE. It lets one approval authorize unlimited executions, and its grant scopes can't match the glob patterns the YAML uses. A "pending" grant is enforceable the moment it's written. The customer-comms send path doesn't go through the trigger at all. The dedup index omits `human_owned`, so duplicates spawn new cases exactly when a human is working the incident. Details in §2.
2. **The CHANGELOG claims fixes that aren't in the spec.** At least eight "accepted → landed at X" rows point at text that doesn't say what the CHANGELOG says (§3). That's a new kind of trust problem: reviewers will read the CHANGELOG, not 800 lines of ARCH, and it's currently the least accurate document in the repo.
3. **Diminishing returns.** Three rounds have produced about 1,600 lines of design and zero lines of code. Most of the remaining open risks are **empirical**, and no further review round can settle them: the ServiceNow assignment model, poll semantics, pre-classifier conflict rates on real tickets, whether the Wilson gates can be met, and how long partner data access takes (§6). **Recommendation: fix the §2 defects, freeze the docs, and build v0.0.** Thresholds chosen before seeing data, such as 0.90, 0.95 and 80%, should become "set from v0.0 shadow data".

---

## 1. Scorecard: round-2 findings

| Round-2 item | Status | Notes |
|---|---|---|
| 2.1 INV-01 CHECK insufficient | ✅ Mostly fixed | The trigger is good. New holes in §2.1–2.4. |
| 2.2 Customer comms bypass INV-01 | ⚠️ Partial | `comms_plans` exists, but `comms` has no trigger and no CHECK, and plan approval doesn't use `approvals` (§2.5). |
| 2.3 `check:` eval hole | ✅ Fixed | CEL, compiled in CI. One binding bug (§2.9). |
| 2.4 Redaction coverage | ✅ Fixed | Inside `LLMClient`. The canary test risks being tautological (§2.11). |
| 2.5 Case-before-dedup | ✅ Fixed in intent | The upsert SQL and the pseudo-code have bugs (§2.6). |
| 2.6 RCA iterates hypotheses | ✅ Fixed in intent | An indentation bug makes the no-runbook path always return early (§2.8). |
| 2.7 `$case.resource` overloaded | ⚠️ Partial | Service catalog added, but the runbook example still compares `r.repo == case.resource` (§2.9). |
| 3.1 Broken xrefs | ✅ Fixed | §4.2, §5.5, §8.3 and §8.4 now exist. |
| 3.2 `work_note` vs `customer_comment` | ⚠️ Partial | Split in ARCH, but C7 param collection is still a "T1 comment" to the requester, and `watch_list` is T1 in CAP but T2 in the demo (§4). |
| 3.3 LLM paging | ⚠️ New contradiction | `create_page` is T2 *gated*, so a page waits for approval (§2.10). |
| 3.4 Conflict → safe default | ✅ Accepted with pushback | The pushback is reasonable. `calibrated_score` has an open-world problem (§2.7). |
| 3.5 Material change → second case | ⚠️ Partial | Fixed for severity. "New alert_type / new resource" can't reach the in-place path (§2.6). |
| 3.6 Self-echo / takeover | ⚠️ Partial | Echo filter added. Assignment automation and the poll granularity problem are new (§5.1–5.2). |
| 3.7 C14 headline | ❌ Not fixed | The ROADMAP v1.0 exit demo (L211) is still the C14 self-change demo. |
| 3.8 Shadow MTTR | ✅ Fixed | |
| 3.9 YAML vs grants | ⚠️ Partial | The schema can't represent a pending grant (§2.3). |
| 4.1 v0.1 scope | ⚠️ Partial | Two-person rule, DLP and PITR moved out. Phase 5 still lists all three evidence families (§3). |
| 4.2–4.4 v0.0 / partner / team | ✅ Fixed | Team size stated. v0.0 depends on identity that arrives in v0.1 (§2.12). |
| 5.1 Entailment | ✅ Fixed | Fail-closed, budgeted, evaluated. |
| 5.2 Cache key | ✅ Fixed | Needs a frozen KB snapshot (§2.13). |
| 5.3 Gates don't gate | ⚠️ Over-corrected | The new Wilson gates can't be passed with the planned suite size (§2.13). |
| 5.4 Durability | ✅ Fixed | Poll-based sync has its own loss modes (§5.2). |
| 5.5 Missing items | ✅ Mostly fixed | Retention, crypto-shredding, SLO, spend breaker and users/proposals are all present. Storm grouping still has no mechanism. |
| §6 Docs | ⚠️ Partial | ARCH is mostly clean. ROADMAP still has review prose (L7, L89, L112, L146, L147) and wasn't slimmed. |

---

## 2. Spec-level defects in the new SQL and pseudo-code

### 2.1 INV-01 is enforced on INSERT only
`trg_outbox_inv01` is `BEFORE INSERT` (ARCH L374). Nothing stops a later `UPDATE outbox SET payload_json = …, payload_hash = …` or `SET approval_id = …` on a `pending` row. The executor's CAS update (`status: pending → claimed`) is an UPDATE, so any bug or compromise in that path skips the invariant.
**Fix:** add a `BEFORE UPDATE OF payload_json, payload_hash, approval_id, policy_grant_id, scope, action_kind ON outbox` trigger that aborts, which makes those columns immutable after insert. Have the executor **recompute `h(payload_json)` before sending** and abort on mismatch. The trigger compares hashes, but nothing in the DB ties `payload_hash` to `payload_json`.

### 2.2 One approval can authorize unlimited executions
The trigger accepts `p.status IN ('approved', 'executed')` (L381). After the first send marks the proposal `executed`, the **same approval still validates new outbox rows** with different idempotency keys. The idempotency key stops *retries*, not *new rows*.
**Fix:** `UNIQUE(outbox.approval_id)` for single-shot kinds, and only `p.status = 'approved'` in the trigger, with the transition to `executed` happening in the same transaction as the insert. If some kinds legitimately fan out, declare that per kind.

### 2.3 Grants: pending rows are live, and scopes can't match patterns
- `policy_grants.active DEFAULT 1`, and `granted_by`, `granted_at_ts` and `valid_to_ts` are all `NOT NULL` (L342–351). ARCH §6.4 says `grants request` writes a *pending* row that becomes enforceable only after admin approval. **The schema can't express a pending row.** You either supply a fake `granted_by` or the row is active on insert. Add `status (requested|approved|revoked)` and a `requested_by` column, make the approval columns nullable until approved, and have the trigger require `status = 'approved'`.
- "Admin ≠ requester" (CHANGELOG 5.5) can't be enforced without `requested_by`.
- The trigger uses `g.scope = NEW.scope`, an **exact string match**. The YAML requests use patterns (`"workflow:deploy/**, job:build"`, CAP L55). Either every grant is a literal (and the docs' examples are wrong), or matching happens in code and the trigger is no longer the enforcement point. Pick one. Literal scopes are simpler and auditable.

### 2.4 `customer_facing_kinds` doesn't include `create_page`, and it's mutable data
The seed list is `customer_comment, send_email_customer, create_ticket_customer, …` (L355). ROADMAP L134 and L238, CAP L36 and PITCH all say `create_page` is *never grant-allowlistable* "because the code-owned table enforces it". It isn't in the table. Kind names also drift across docs: `send_email` (ROADMAP L134) vs `send_email_customer`, and `create_ticket` vs `create_ticket_customer`. A table also isn't "code-owned": any DB writer can `DELETE` from it.
**Fix:** inline the list as literals in the trigger (generated from a Python constant at migration time, with a CI test that the two match), add `create_page`, and define a single canonical action-kind enum, CI-checked across the docs and code.

### 2.5 The customer-comms send path has no enforcement
INV-01 covers "customer-facing actions", but customer sends are rows in **`comms`**, not `outbox`. `comms` has `plan_id` and `approval_id`, with:
- no CHECK requiring either one;
- no trigger validating that the plan is `approved` and not `revoked`, or that the approval is valid;
- `comms_plans` approval stored as an `approved_by` column rather than an `approvals` row. RBAC, no-self-approval and payload binding all go through `approvals`/`proposals`, so plans get none of them, and a plan has no `payload_hash` for what the approver saw.

`test_customer_comm_never_by_policy_grant` is trivially true because `comms` has no grant column. It tests nothing. The CHANGELOG (round-2 2.2) says *"every `comms` row references a plan with the same validity trigger"*. The DDL doesn't contain that.
**Fix:** route all sends through `outbox` (comms becomes a planning table), *or* give `comms` its own trigger. Make plan approval a proposal/approval pair with a hash over `{template_id, allowed_fields, cadence, audience}`.

**A design point to add:** "enum-only fields are safe to auto-send" depends on *where the enum value comes from*. Telling a customer `status = resolved` is the most consequential thing you can say. If the agent infers it from an LLM classification and auto-sends under the plan, an LLM decision reaches the customer unreviewed. Require that auto-sent field values come from **ServiceNow record state set by a human**, not agent inference.

### 2.6 Dedup upsert and pseudo-code bugs
- `RETURNING id, (changes() = 0) AS created` (L147) doesn't work. `changes()` reports the *previous* statement and returns 1 for both the insert and the update paths. There is also a stray `;` before `ON CONFLICT` (L144). Use a sentinel instead, for example `RETURNING id, duplicate_count = 0 AS created` (or `xmax = 0` on Postgres).
- `case.duplicate_count += 1` in Python (L94) runs after the SQL already incremented, so each duplicate is counted twice.
- **"New alert_type / new resource" can't be a material change.** Both are fingerprint components (§4.1), so a new value means a different fingerprint, which means a new case, and the in-place path never sees it. Only severity escalation reaches `material_change`. Either take those fields out of the fingerprint, or drop them from the material-change list and explain how cross-resource escalation links up (that's storm grouping, which is still unspecified).
- **There's no time window in the index.** ROADMAP says "sliding 1h window". The partial index has no time component, so a case open for three days absorbs every matching alert. That may be the right behavior, but then remove "sliding 1h" from ROADMAP.
- **`human_owned` and `reopened` aren't in the index's status list** (L141 vs lifecycle L475). Once a human takes over, the next duplicate alert **creates a second open case** for the incident the human is working, which violates INV-02 at exactly the worst moment. Index on "not resolved" instead.
- **Human tickets collapse.** "Ticket bodies are not fingerprinted to error signatures" means a human ticket's fingerprint is `(source, resource, alert_type)`. Two different customers filing about the same service and category get **merged into one case**, violating "two distinct tickets must never collapse". For human-filed tickets the fingerprint must include ticket identity (`number`).
- Postgres is "supported via a flag", but the trigger, the upsert and `strftime` are SQLite dialect. INV-01 needs a PL/pgSQL twin, and **the invariant suite must run against both engines**, or drop Postgres from v0.x.

### 2.7 `calibrated_score` is undefined exactly where it matters
`calibrated_score` = pre-classifier match + prefix agreement + per-label base rate. *"If no calibrated inputs exist, the score floors at the safe-default threshold"* (L158). On free-text tickets the pre-classifier abstains, and those are most of app-support volume. Either:
- "floors at" means `== threshold`, which passes `< min_score`, so the **unchecked LLM label is accepted precisely when there is no cross-check** (fail-open), or
- it means below the threshold, and **every free-text ticket goes to safe-default**, so the product only works on machine alerts.

Say which one. The honest answer is probably "free-text tickets route on the LLM label with a stricter severity cap (no P1 routing without pre-classifier agreement)". The CHANGELOG also describes `calibrated_score` as "N-sample agreement", which isn't in ARCH (§3).

### 2.8 The RCA no-runbook path always returns early
```python
        plan = [(h, PROBES[h]) for h in proposed if h in PROBES]
        if not plan:  trace.decision("rca", "no_runbook_no_probes")
        return RCAArtifact(outcome="INCONCLUSIVE", reason="no testable plan")   # L186: always runs
```
The `return` sits at the `else` level, not under `if not plan`. Every case without a runbook returns INCONCLUSIVE and never runs a probe. Also, `contradicts` from entailment is said to feed `reduce()` (L279), but entailment runs *after* `reduce()` (L196–199).

### 2.9 Runbook binding is still inconsistent, and "CONFIRMED" overstates
- `args: {repo: "$case.resource"}` is "bound via the service catalog", but `check: … r.repo == case.resource` compares a repo name with a service name, so it will be false whenever they differ. Bind as `case.service.repos` in both places.
- **`CONFIRMED` is too strong a label for the evidence.** s1's check is "any failed or erroring CI run in the repo in 24h". In a busy repo that's nearly always true, and it isn't evidence of a *deploy*, let alone causation. With `stop_if: true` the loop stops at the first weak correlation, and `comms_if_confirmed: {audience: [customer]}` can turn it into a customer message. Rename the outcome to `SUPPORTED`, require **two independent signals** (for example a change *and* a temporal match with the error onset), or keep customer comms off RCA outcomes entirely in v0.x.

### 2.10 `create_page` is gated, so it doesn't page
`create_page` is "T2, gated" (L612) and also *"the fallback if a deployment has no external monitoring"* (L613). A page that waits for a human approval doesn't wake anyone at 3 a.m., which is the round-1 problem in reverse. Also, *"not LLM-decidable"* isn't quite true: the conditions use the LLM's `severity = P1` and an LLM-classification-matched runbook, and `calibrated_score` includes LLM agreement.
**Fix:** say exactly what it is. For example: *"`create_page` is a rule-authorized T1 action (no human gate) that fires only when the deterministic pre-classifier independently yields P1 and the LLM agrees; rate-limited."* Or remove `create_page` from v0.x and make external monitoring a deployment prerequisite. That's simpler and honest.

### 2.11 The INV-06 canary test can be tautological
If the canaries are generated from the same patterns the detectors match, the test proves only that the regexes match themselves. Detector *recall* on realistic secrets is the real property: keys embedded in URLs, base64-wrapped tokens, JDBC strings with passwords, cloud credentials split across log lines.
**Fix:** keep a held-out corpus of realistic secrets that the detector authors don't see, and track recall as a quality metric. INV-06 stays the hard gate for known classes.

### 2.12 v0.0 needs identity that arrives in v0.1
The v0.0 exit requires approvals in the admin UI (ROADMAP L178), and the schema makes `approvals.approver_id` an FK to `users`, keyed by OIDC `sub`. But **OIDC login is in Phase 2 (weeks 5–7)**. The `work_note` T1 auto-post in v0.0 also needs an approved `policy_grants` row, which needs an admin user and `granted_by`. Either v0.0 ships a local-account auth stub, which needs its own threat model, or the v0.0 demo can't satisfy its own invariants. The same issue applies to *"T1 policy-allowed by default"*: a default needs a seed grant, and a seed grant has no human `granted_by`, which contradicts "customer-owned". Pick one: a first-run setup flow where the customer admin approves the default T1 grants, recorded as a real grant.

### 2.13 The release gates can't be passed with the planned suite
Wilson 95% lower bound, *with a perfect score*: LB = n / (n + 3.84).

| Gate | Cases needed for a perfect score | To survive one miss |
|---|---|---|
| P1/P2 classification ≥ 0.90 (L542) | **≥ 35** P1/P2 cases | ≈ 52 |
| Citation faithfulness ≥ 0.95 (L545) | **≥ 73** claim/evidence pairs | ≈ 100 |
| "≥ 5 holdout cases per label" | 5/5 → LB = 0.57 | — |

ROADMAP Phase 3 still plans **25–50 cases total**, and P1/P2 is a minority of them. **As written, v0.1 can't ship.** Also, N samples per case aren't independent observations, so compute the Wilson bound over *cases* (using a majority vote per case), not cases × N.
**Fix:** either plan the suite size to the gate (about 150–250 cases, which the partner corpus can supply), or set thresholds from v0.0 shadow data and say so.

Related: the cache key is the rendered-prompt hash, and the rendered prompt includes retrieved case memory. Unless evals run against a **frozen KB/case-memory snapshot**, every new resolved case changes prompts, invalidates the cache, and makes eval results non-reproducible. Version the snapshot alongside the suite.

Also related: the pinned-model examples (`gpt-4o-2024-11-20`, `claude-3-5-sonnet-20241022`, ARCH L512) are already past provider retirement. Pinning is right, but it means **scheduled forced migrations**. Add a model-deprecation runbook: re-baseline the quality suite on the successor, diff, then human sign-off.

---

## 3. CHANGELOG ↔ spec discrepancies

The CHANGELOG is now the document most people will read first. These rows claim fixes that the normative docs don't contain:

| CHANGELOG claim | Actual spec |
|---|---|
| R1 3.1: `calibrated_score` = "N-sample agreement" | ARCH §4.2 defines it with no N-sampling. |
| R2 2.2: "every `comms` row references a plan with the same validity trigger"; `comms_plans` "carry `approval_id`/`payload_hash`" | No comms trigger, no CHECK; `comms_plans` has neither column (ARCH L410–436). |
| R2 2.2 / R2 3.3: `create_page` structurally excluded via `customer_facing_kinds` | Not in the seed list (ARCH L355). |
| R2 3.6 / R1 5.6: "cadence updates under `human_owned` remain in draft" | ARCH §8.2 says the agent "stops proposing and sending"; drafts aren't mentioned. |
| R1 7.6 / R2 4.1: "v0.1 ships **one** change source; APM + reranker land v0.2" | ROADMAP Phase 5 (L191) still lists the change feed (Argo/Terraform/Consul/CMDB), the feature-flag source **and** metrics/APM in v0.1. ARCH §5.2 presents the cross-encoder without a release tag. |
| R1 §10 / R2 §6: "ROADMAP §2 slimmed to links + exit criteria"; review prose removed | ROADMAP §2 is still full design detail, with "earlier draft was wrong / real bug" at L7, L89, L112, L146 and L147. |
| R2 3.7: "v1.0 exit story = C22 assisted delta" | ROADMAP L211 v1.0 exit demo: *"the agent proposes a change to itself…"* |
| R2 2.7: runbook params "bound **at registration**" | ARCH says bound **at runtime** via the catalog (L191, L234). These are different designs. |
| R2 5.5: `proposals` table "accepted" | ✅ It's there, but the no-self-approval check (§6.4) has no constraint or trigger shown. |

**Fix:** generate the CHANGELOG's "where it landed" column as anchors, and add a doc-lint CI step that fails if an anchor doesn't resolve. Better still, cut the CHANGELOG to the decisions and pushbacks (the valuable part) and stop restating fixes.

---

## 4. Remaining cross-document contradictions

- **The non-learnable surface differs within CAP itself.** Design rule 5 (L24) lists gate, tiers, permissions, policy and eval structure. §4.6 (L238) adds RBAC, routing and taxonomy. ARCH §10 matches L238. Update L24.
- **C7 param collection is still a T1 comment** to the requester (CAP L210, ROADMAP Phase 8 L200). That message goes to the requester, so it's a `customer_comment`, which is T2 and never grant-allowlistable. Either every parameter question is gated (fine, but say so and budget the approval load), or the param ask moves to a channel that isn't customer-facing.
- **`watch_list`** is T1 in CAP L35 ("add-to-watchlist") but T2-gated in the demo (ROADMAP L217). Adding a *customer* to `watch_list` sends them emails, so it's customer-facing.
- **Send idempotency key "`UNIQUE` in the outbox"** (CAP L115). In ARCH it's on the `comms` table.
- **The shadow promotion gate**: "≥80% over the last 30 cases" (CAP L248, ROADMAP L254) with no lower bound. At n=30, 80% observed gives a Wilson lower bound of about 0.63, while §11 gates everything else on lower bounds. Use one rule.
- **Duplicate failure table**: the INV-05 per-stage table appears twice (ARCH L117–125 and L160–168).
- **The diagrams** in ARCH §3 and ROADMAP §1 still show `[redact]` as an inbound stage before dedup, and ARCH §6.1 says it isn't a stage.
- **PITCH reintroduces overclaims** that were retracted from the spec: *"regression-proof by construction"* (L57) and *"Every regression is caught in CI before it ships"* (L78). The quality gate is statistical, so some regressions will pass. Customers read PITCH, not ARCH. Hold PITCH to the spec's honesty standard: "regressions in invariants are blocked; quality regressions beyond the baseline band are blocked."

---

## 5. Operational risks the design can't resolve on paper

### 5.1 Assignment automation will trigger "takeover" immediately
In most ServiceNow deployments, **assignment rules or predictive intelligence set `assigned_to` to a human at creation**, or within minutes. The takeover trigger is "a human assigns the ticket". The automation runs as `system` or as the creating user, not as the integration user, so the self-echo filter doesn't catch it. Result: **every ticket becomes `human_owned` before the agent proposes anything**, and the product goes quiet by design.
**Fix:** define takeover relative to the partner's assignment model, for example "a human *actor* (not `system` and not a known automation user) changes `assigned_to` or `state`". Make the actor allowlist part of `doctor`. This needs real partner data to get right, which is a good reason to build v0.0 first.

### 5.2 Poll-cursor sync has its own loss and visibility modes
- `sys_updated_on` has **one-second granularity**, and commits can land out of order. A strict `> last_seen_cursor` drops records updated in the same second as the cursor. Use `>=` with an overlap window, and dedup on `(sys_id, sys_mod_count)`.
- **Polling sees current state, not history.** If a human changes `state` and the integration user writes a work note within one poll interval, the poll sees `sys_updated_by = integration user`. The echo filter skips it, and **the human's takeover is invisible**. Detecting *who changed what* needs `sys_audit` / `sys_journal_field` history, not just the record.
- The poll interval adds directly to MTTA and to the "alert → first proposal" SLO. Event Management alerts live in `em_alert`, a separate table with its own cursor. Polling several tables per tenant also eats into ServiceNow rate limits.
- "No webhook-loss window exists" is true. "No loss window" isn't.

### 5.3 The assisted delta needs a control group
"MTTA/MTTR vs the customer's human-only baseline" as a before/after comparison is confounded by seasonality, staffing, incident mix and the partner's own process changes. For a number that goes into a renewal conversation, hold out some services or shifts as unassisted controls during the same period. Also, **promotion to assisted mode on production is the partner's decision**. It's a commercial milestone, not an engineering one, and the plan should treat it as a dependency like data access.

### 5.4 Storm grouping is still one sentence
C20 is "deterministic time+resource grouping" with no table, no group/parent-case model, and no rule for cross-resource cascades. It's the main v0.1 storm defense and a CI soak gate, so it needs at least a schema (`case_groups`) and a grouping rule before the soak test can mean anything.

---

## 6. The meta-point: stop designing, start measuring

Many of the remaining open questions **can't be resolved by another document revision**:

| Question | Answered only by |
|---|---|
| What is the pre-classifier conflict / abstain rate? | partner tickets |
| Does takeover detection work with their assignment rules? | their instance |
| Are 0.90 / 0.95 / 80% achievable? What suite size? | a v0.0 shadow run |
| Does `calibrated_score` fail open or closed in practice? | real free-text tickets |
| How long does data access take legally? | sending the request (day 0) |
| Is the poll latency acceptable against the SLO? | their instance's rate limits |
| Does plan-level approval actually reduce approval load? | assisted-mode pilots |

The spec is now detailed enough that it **fixes numbers before data exists**, and each extra round adds surface area (now 800 lines of ARCH) that the code will have to match or the docs will drift again. Concretely:

1. Fix §2.1–2.6, §2.8 and §2.10 in the spec. These are correctness bugs in the core trust chain, and they're cheap to fix now.
2. **Freeze ARCH.** Further changes go through ADRs tied to code PRs.
3. Turn every `test_*` name in ARCH into an actual failing test in week 1. The invariant suite *is* the spec from then on.
4. Mark every numeric threshold as "provisional, set from v0.0 shadow data".
5. Trim the CHANGELOG to decisions and pushbacks, and lint its anchors in CI.

---

## 7. Top 10 actions, in order

1. **Close the INV-01 holes**: an immutability trigger on UPDATE, single-use approvals (`UNIQUE approval_id`, `status = 'approved'` only), executor-side hash recomputation (§2.1, §2.2).
2. **Fix the grant schema**: `status` and `requested_by` columns, nullable approval fields, literal scopes only (§2.3).
3. **Inline `customer_facing_kinds` as trigger literals**, add `create_page`, and use one canonical action-kind enum (§2.4).
4. **Put customer sends under the trigger**: through `outbox`, or a `comms` trigger; plan approval as proposal + approval with a hash; enum values only from human-set record state (§2.5).
5. **Fix dedup**: index on "not resolved" (covering `human_owned`), ticket identity in human-ticket fingerprints, fix the upsert's `created` detection and the double count, reconcile the material-change list with the fingerprint (§2.6).
6. **Fix the RCA bugs**: the indentation early-return, entailment ordering, repo binding; rename CONFIRMED or require two signals before customer comms (§2.8, §2.9).
7. **Decide what `create_page` is**: rule-authorized T1 or out of v0.x. Define `calibrated_score`'s fail-open/closed behavior for free text (§2.7, §2.10).
8. **Make the gates achievable**: size the suite to the Wilson math or make thresholds provisional; freeze the KB snapshot for evals; add a model-deprecation runbook (§2.13).
9. **Reconcile the CHANGELOG with the spec** (all §3 rows) and the cross-doc contradictions (§4), including the PITCH overclaims and the ROADMAP v1.0 exit demo.
10. **Freeze the docs and build v0.0** with the invariant tests first. Take §5.1–5.2 (assignment automation, poll semantics) to the design partner's instance as the first empirical questions (§6).
