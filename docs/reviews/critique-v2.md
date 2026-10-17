# SupportAgent: Critical Review, Round 2

**Scope:** the revised `ARCHITECTURE.md` (now normative), `CAPABILITIES.md` and `ROADMAP.md`, reviewed against round 1 ([critique.md](critique-v1.md)). There is still no code, so this is still a design review.
**Stance:** adversarial again. Round 1 asked "what will break?" This round asks "did the fixes actually fix it, and what did the fixes break?"

---

## 0. Verdict

The revision is a real improvement in **honesty**. The overclaims were retracted, the guarantees became five numbered invariants with test IDs, and there are now non-goals, a taxonomy, a paging rule, a train-on-test guardrail and an explicit design-partner dependency. Those are the right moves.

The revision has three systemic problems:

1. **Several fixes exist only in prose.** The pseudo-code, the schema and the constraints don't implement what the text claims. INV-01's CHECK constraint doesn't stop execution on a *rejected* approval. The triage code creates a new case *before* the dedup check, which breaks INV-02 by construction. The RCA loop still iterates hypotheses rather than steps, even though the doc says the bug was fixed.
2. **The scope went up, not down.** Round 1 said the 10-week plan was 6–12 months of work. The revision kept 10 weeks and **added** redaction, sync, storm grouping, RBAC with a two-person rule, outbound DLP, entailment checks, a cross-encoder reranker, webhook auth, backup with point-in-time recovery, and **seven new evidence integrations** (Argo CD/Terraform/Consul/CMDB, a flag store, Prometheus/Datadog/CloudWatch, on-call). Phase 5 is now three weeks for roughly ten capabilities.
3. **The docs read like a response to the review, not a spec.** Phrases like "was a real bug", "the earlier text was wrong" and "this fixes an unsupportable claim" are changelog material inside the normative spec. The duplication that caused round-1 drift has *grown*: ROADMAP §2 still restates outbox, dedup and event-kind designs while saying it "never restates". Four section references in ARCHITECTURE point to sections that don't exist.

Net: **the design is now defensible in a review meeting, but it can't be built in the stated timeline, and the invariants as specified would pass CI while being violated.**

---

## 1. Scorecard: round-1 findings

| Round-1 item | Status | Notes |
|---|---|---|
| Promise contradicted by T1/allowlist (§2.1) | ✅ Fixed | The promise is stated once and consistently: T2+ gated, T1 policy-allowed, every execution traces to a human or a policy. |
| SN fingerprint = `number` (§2.3) | ✅ Fixed in prose | Identity and fingerprint are now separate. Its definition still conflicts with INV-02 (see §3.5). |
| `count` column missing (§2.4) | ✅ Fixed | `duplicate_count`. |
| Two event schemas (§2.5) | ⚠️ Partial | ARCH §15 is canonical, but ROADMAP §2.3 still restates the full kind list, so it will drift again. |
| Multi-tenant / Helm in v0.1 (§2.6) | ✅ Fixed | Deferred; `tenant_id` is in the schema. |
| Fixture CI can't catch prompt regressions (§2.7, §4.1) | ✅ Mostly fixed | Real LLM, N samples, cache invalidation. The cache key is wrong (§4.2). |
| Diagram lists unscheduled adapters (§2.8) | ✅ Fixed | |
| "Bad prompt can't move the pathway" (§3.1) | ⚠️ Partial | Honestly rescoped, with a pre-classifier added. Calibration is *named* but never specified, and the pseudo-code still gates on self-reported `confidence` (ARCH L96). |
| NL `expected_evidence` (§3.2) | ⚠️ Partial | Now machine predicates, but as strings that get eval'd: a code-execution hole (§2.3). |
| Citation ≠ support (§3.3) | ⚠️ Partial | An entailment check was added, with no mechanism, failure default or eval of its own. |
| Exactly-once overclaim (§3.4) | ✅ Fixed | Effectively-once plus reconciliation, honestly stated. |
| Template leak (§3.5) | ✅ Fixed | Field allowlists, with LLM text only as a human-edited draft. |
| Outbox "DB-enforced" (§3.6) | ❌ Not actually fixed | The CHECK only tests non-null (§2.1). |
| Inbound injection (§3.7) | ✅ Fixed in principle | The target-relates-to-resource validator is undefined, but it has been named. |
| Dedup window / severity swallowed (§3.8) | ⚠️ Changed, new bug | "Material change → *new* linked case" splits one incident into two cases and contradicts INV-02 (§3.5). |
| Fixture vs prompt testing, eval size (§4.2) | ⚠️ Partial | N-sampling added, but the suite is still 25–50 cases. Shadow gate of N≥30 with a confidence band: the band is never used as the gate (§4.3). |
| Real-incident corpus honesty (§4.3) | ✅ Fixed | The design partner is primary, and the framing is honest. |
| CC BY-NC-ND (§4.4) | ⚠️ Partial | Appendix D is still used as an eval "structural reference", and the pitch closer still leans on it. |
| Prior weighting eval mix (§4.5) | ✅ Fixed | |
| Agent in paging path (§5.1) | ⚠️ Over-corrected | Now the agent can *page* ungated on an LLM label (§3.3). |
| Approval fatigue (§5.2) | ✅ Fixed in design | Plan-level approval. The schema can't represent it yet (§2.2). |
| Storm handling (§5.3) | ⚠️ Partial | Deterministic grouping by time and resource doesn't group *cascades* across resources, and that's what storms are. |
| Comment-approval hole (§5.4) | ⚠️ Inconsistent | ARCH says not valid for state changes, ROADMAP says T1 only. T1 doesn't need approval, so the feature is dead code (§3.2). |
| RBAC (§5.5) | ⚠️ Prose only | No users/roles tables. `approved_by` in the allowlist is still free text, and `expires: 90d` is still unanchored. |
| Sync / takeover (§5.6) | ⚠️ Partial | Lifecycle added. The self-echo loop and the takeover definition are missing (§3.6). |
| Durability (§5.7) | ⚠️ Partial | DB lease and Alembic added. Webhook redelivery is still *assumed* (§5.4). |
| PII redaction (§6) | ⚠️ Partial | Only on the classify call. RCA sends logs to the LLM unredacted (§2.4). |
| Webhook auth | ⚠️ Partial | In ROADMAP Phase 2 only, absent from the normative spec. |
| Data retention / GDPR | ❌ Not addressed | |
| Cross-tenant leakage | ✅ Fixed | `tenant_id` everywhere. |
| Comms exfiltration / DLP | ✅ Fixed in design | |
| `cron` re-invoke | ✅ Fixed | Deferred. |
| Pseudo-code bugs (§7.1) | ⚠️ Partial | Three fixed, two new, one claimed fixed but still present (§2.5–2.6). |
| Runbooks as YAML (§7.2) | ✅ Fixed | `check` expression language problem (§2.3). |
| Retrieval specified (§7.3) | ✅ Fixed | Signature normalization is a good addition. |
| Learning-loop leakage (§7.4) | ⚠️ Partial | Holdout added. "Disjoint by incident" is still leaky, and CAP §4.6 still says "cases generated from source incidents" (§3.7). |
| C14 de-prioritized | ⚠️ Contradicted | "Not a v0.x headline", but it's still the **v1.0 exit demo** (= v0.3) (§3.7). |
| Evidence sources vs prior (§7.6) | ✅ Over-fixed | Fixed by adding seven integrations to v0.1 (§4.1). |
| Scope / re-cut (§9) | ❌ Worse | §4. |
| Docs: pitch vs spec, duplication (§10) | ⚠️ Partial | Non-goals and positioning added. "Money shot" and "marketing sentence" remain, and duplication grew. |

Score: about 14 fixed, 17 partial, 3 not fixed or worse.

---

## 2. New defects: the spec doesn't do what it says

These matter most, because the docs now claim they're *fixed*.

### 2.1 INV-01's CHECK doesn't enforce INV-01
```sql
CHECK ( (approval_id IS NOT NULL) OR (policy_grant_id IS NOT NULL) )   -- ARCH L233
```
This checks that a reference *exists*, not that it is **valid**. All of these pass the constraint:
- an outbox row pointing at an approval with `status = rejected` or `expired`;
- an outbox row pointing at a `policy_grants` row whose `valid_to_ts` is in the past, or whose `scope` doesn't cover this target;
- an outbox row whose payload differs from the approved `payload_hash`. "FK to the bound payload_hash" is asserted, but neither table has a column to bind it to.

A CHECK constraint can't reference other tables. On SQLite, **foreign keys are off unless `PRAGMA foreign_keys=ON` is set on every connection**. So "rejected at the DB layer" (ROADMAP L131) is still an overclaim.

**Fix:**
- Make the FK composite: `outbox(approval_id, payload_hash) → approvals(id, payload_hash)`.
- Add an `approvals` status guard via a trigger (`BEFORE INSERT ON outbox … SELECT RAISE(ABORT) WHERE status <> 'approved'`) and an equivalent trigger for grant validity and scope.
- Enforce `PRAGMA foreign_keys=ON` in the engine-connect hook.
- Add test cases for *rejected*, *expired*, *hash-mismatch* and *expired-grant* rows, not only orphans. `test_gate_no_orphan_execution` tests the weakest version of the invariant.

### 2.2 Customer comms bypass INV-01 entirely
INV-01 says *"no state-changing **or customer-facing** action executes without a matching approval/grant row"*. But customer sends go through the **`comms` table**, which has **no `approval_id` and no `policy_grant_id`** (ARCH L367). Plan-level approval, the anti-fatigue fix, also has nowhere to live. `approvals` is keyed per `action_kind + payload_hash`, and a *plan* isn't a payload.

This creates a second conflict: *"customer-facing comms are never allowlistable"* (CAP §2), yet plan-approved updates **auto-send** (CAP L117). An approved plan that auto-sends is functionally an allowlist with a human signature on it. That can be the right design, but say so.

**Fix:**
- Add a `comms_plans(id, case_id, audience, template, allowed_fields, cadence, approval_id, payload_hash)` table.
- Make every `comms` row reference a `comms_plan_id`, with the same validity trigger as §2.1.
- Restate the rule as: *customer comms require a human-approved plan or a human-approved message; no policy grant can authorize them.*

### 2.3 Runbook `check:` strings are a code-execution path
```yaml
check: "any(r.repo == $case.resource and r.state in ['failed', ...] for r in $result)"   # ARCH L174
```
This is a Python expression in a string. Evaluating it means `eval()` or a hand-rolled interpreter. In v0.2, runbooks are **LLM-distilled** (C13), so model-authored code would run inside the process that holds ServiceNow credentials. Human review at T3 is the only barrier, and reviewers don't read expression strings carefully.

**Fix:** use a non-Turing-complete, sandboxed expression language with a schema. [CEL](https://cel.dev) (`cel-python`) or JMESPath plus comparison operators are good options. Validate in CI that every `check` compiles against the tool's declared result schema.

### 2.4 Redaction covers one call; RCA leaks logs to the LLM
`redact()` wraps only `llm.classify` (ARCH L94). Every other LLM call receives **unredacted** content:
- `hypotheses()`, `conclude()` and the `supported_by()` entailment check (ARCH L132–148);
- the LLM-judge reranker (§5.2);
- the comms drafter.

These calls consume **tool output: logs, CI logs and metrics labels**, which is where secrets actually live (connection strings, bearer tokens in request logs, customer emails in stack traces). The ticket body is the *least* secret-dense input in the system.

**Fix:** put redaction **inside `LLMClient.structured()`** as a mandatory pre-send filter, so no call path can skip it. Add an INV-06: *no LLM-bound payload contains a detector-positive secret* (tested with canaries planted in fixture logs).

Also retract *"PII in the ticket body demonstrably never reaches the LLM (proven by trace)"* (ROADMAP L191). Pattern-based redaction catches structured PII (emails, keys, card numbers) but not names, addresses or free-text health data. The trace can prove only that *detected* PII was removed.

### 2.5 `triage()` creates a case before checking dedup, which breaks INV-02
```python
case = cases.create(tenant=evt.tenant, fingerprint=fingerprint(evt))   # L87: every alert makes a case
if dup := dedup.find(evt.tenant, evt.fingerprint, ...):                # L89: then checks dedup
    dedup.link(case, dup.case); return dup.case
```
1,000 duplicate alerts produce 1,000 `cases` rows. The 1,000-alert soak asserts *"exactly N distinct cases"*, so as written **the soak would fail**, or worse, the test counts "linked" cases differently and passes. Two smaller issues: `fingerprint(evt)` is computed but `evt.fingerprint` is what gets looked up, and the dedup check isn't atomic. Two concurrent identical alerts both miss, and both create cases.

**Fix:** dedup first, then `INSERT … ON CONFLICT (tenant_id, fingerprint) WHERE status IN ('open', …) DO UPDATE SET duplicate_count = duplicate_count + 1 RETURNING id`, backed by a **partial unique index** on open cases. That makes INV-02 a DB property, which is what the invariant table claims ("dedup + unique index"), but the index isn't in the schema.

### 2.6 RCA still iterates hypotheses, not steps, and the doc says it was fixed
ARCH L128 claims *"it is one algorithm, not two"* and L184 says *"Loop B iterates `steps[]`"*. The code at L137–139 is still `for h in hyps: step = steps.step_for(h)`. Additional problems:
- `steps` is a list (`rb.steps`), and lists have no `.step_for`;
- runbook steps declare hypothesis IDs (`recent_binary_or_config_push`), but `hypotheses()` is LLM-generated free text. Nothing maps the two vocabularies, so `step_for(h)` will usually miss. **Hypotheses need a taxonomy too**, like classifications;
- `reduce()`, `conclude()` and `score()` decide the categorical outcome, but they're undefined. The categorical outcome was the fix for the meaningless 0.82, and its rules are the part that matters.

**Fix:** pick one algorithm. The simplest one that matches the YAML: *the runbook is the plan. Iterate `steps` in order; each step tests its declared hypothesis ID; the LLM only proposes extra hypotheses (from a fixed hypothesis taxonomy) when no runbook matches.* Define `reduce()` as a truth table.

### 2.7 `$case.resource` is doing three jobs
The same `$case.resource` is bound as a **GitHub repo** (`repo: "$case.resource"`) and as a **log path** (`path: "$case.resource"`), and it's presumably also an Airflow DAG and a CMDB CI. These are different namespaces. **A service catalog is missing**: a mapping from `service → {repos, log paths, DAGs, CMDB CI, on-call schedule, owners}`. Without it, parameter binding (the fix for hard-coded args) doesn't work, and neither does the "action target relates to case resource" injection validator. Add it as a v0.0 config artifact. It's small, and almost everything depends on it.

---

## 3. Contradictions and design issues introduced by the revision

### 3.1 Broken cross-references in the normative doc
ARCH references **§4.3** (L18, L63, L325), **§5.5** (L132, L156, L258, and CAP L161, L276), **§8.3** (L72, L287, L421) and **§8.4** (L274, L426, L432). **None of these sections exist**: §4 and §8 have no subsections, and §5 ends at §5.4. The pre-classifier spec, the prior-parameter spec and the value-baseline spec, three of the main fixes, are therefore referenced but never written. CAP L118 also labels outbound DLP as **C18**, but C18 is redaction, and DLP has no capability ID.

### 3.2 "Internal ticket comment" is often customer-visible
In ServiceNow, **`comments` (Additional comments) are visible to the caller/requester; `work_notes` are internal**. The docs:
- map outbound comments to `sys_comment` (ROADMAP L91). Records in `sys_journal_field` carry `element = comments | work_notes`, and the doc doesn't say which;
- put *"'please confirm param X' ticket comment"* in **T1** (CAP L35). That message goes to the requester, so it's **customer-facing**, which makes it T2 and never allowlistable. So C7's param collection ("missing param → T1 comment asking") needs a gate on every request;
- have the demo auto-post an "internal ticket comment (T1)" on a ticket the customer may be watching.

**Fix:** make `work_note` (T1) and `customer_comment` (T2) two different action kinds. Have `doctor` verify the field mapping on the partner's instance.

Relatedly, **ticket-comment approval is now dead**. ROADMAP L132 makes it valid "only for T1 actions", but T1 actions are policy-allowed and need no approval. Remove the channel, or say what it approves.

### 3.3 The paging fix over-corrected: the LLM can now page ungated
`escalate`/`page` is **T1, never gated** (CAP L35, ARCH §14), so an **LLM classification can now wake a human at 3 a.m.** Consequences:
- **double paging**: PagerDuty already pages on the source alert, and then the agent pages again;
- **pager DoS through injection**: a customer ticket body crafted to classify as P1 pages on-call. Inbound text is officially "the primary injection surface";
- false pages from misclassification. The cost matrix penalizes P1→P3, but **P3→P1 also has a cost here**, measured in pages.

**Fix:** split the action. `enrich_page` (T1, ungated) attaches context to the *existing* pager incident for the same alert (matched by dedup key). `create_page` stays **gated or rule-only**: it fires only when the *deterministic* pre-classifier says P1, never the LLM alone, and is rate-limited per tenant. That keeps the 3 a.m. guarantee without letting the LLM page anyone.

### 3.4 Classifier conflict → safe default will drown the human queue
*"If the pre-classifier disagrees with the LLM, route to safe-default"* (ARCH L98). Rules and LLMs disagree most on **granularity** (`database/*` vs `database/saturation`), so the conflict rate will be high exactly where the rules have coverage. Meanwhile, on free-text customer tickets (most app-support volume) the rule-based classifier abstains, and the LLM label is unchecked on the hardest inputs. The defense guards the easy cases and leaves the hard ones open.

**Fix:**
- Define "agree" hierarchically (a prefix match on the taxonomy counts as agreement, with the more specific label winning).
- Track the conflict rate and the rule-based classifier's abstain rate as quality metrics.
- Specify the calibration mechanism. §1 promises "confidence calibration", but nothing defines it, and the code uses raw `llmres.confidence`.

### 3.5 Material change creates a second case, which contradicts INV-02
ROADMAP L146: *"material change (severity escalation, …) re-triages into a **new, linked** case"*. But INV-02 says the same `(tenant, source, resource, alert_type)` in the window is **one case**. A P3→P1 escalation has the same key, so by the invariant it must be one case, and by the dedup design it must be two. Operationally, two cases for one incident means split comms, split approvals and duplicate customer updates.

**Fix:** re-triage **in place**. Upgrade the existing case's severity, emit a `decision` event, and re-run routing for the new severity. Separately, INV-02's key `(source, resource, alert_type)` and ARCH §5.2's "normalized *error signature* powers the dedup fingerprint" are two different definitions again. Pick one and write the normalization function down.

### 3.6 Human takeover can be triggered by the agent's own writes
*"A human edits the ticket in ServiceNow → `human_owned`"* (ROADMAP L191). Update events also fire for the **agent's own outbound writes** (they come back through the same business rule), for assignment-rule automations, and for SLA engine updates. Without filtering:
- the agent's first comment echoes back and flips the case to `human_owned`, so the agent silences itself on every case;
- or, if filtered naively, a loop of agent write → event → re-evaluation → write.

**Fix:**
- Filter by `sys_updated_by` equal to the integration user. Define takeover narrowly, as `assigned_to` set to a human, or state changed by a human.
- Define how a case is **handed back** ("human_owned" currently has no exit).
- Decide whether customer cadence updates continue in draft form under `human_owned`. Going fully quiet drops the comms debt on the human.

Also, **how the sync events reach SupportAgent isn't specified**: polling the Table API, or a business rule plus outbound REST in the customer's instance. The second option is a customer-side code change, which usually needs change-board approval, so the *"stranger onboards in <15 min"* exit criterion is only realistic on a developer instance. Say that.

### 3.7 C14 is "de-prioritized" but still the v1.0 headline
ARCH L297 says C14 is *"explicitly not a v0.x headline"*, yet **v0.3 = v1.0**, and its exit demo is exactly C14 (ROADMAP L209). CAP §4.6 still contains the "marketing sentence" and the pipeline text *"EVAL GATE (full suite + **cases generated from source incidents**)"* (CAP L233), which is the train-on-test pattern the holdout rule forbids.

Two more problems with the guardrail:
- **"Disjoint from the source incidents" is leaky.** Recurring problems produce near-duplicate incidents, so a holdout that excludes incident X but includes X's twin from last Tuesday is still train-on-test. Make the holdout disjoint **by normalized signature and by time** (train on before date T, hold out after T).
- **T3 now lists "routing config" as learnable control** (CAP L37), but routing isn't on the non-learnable list. The loop could therefore *propose pathway changes*, which contradicts "code decides control" more directly than anything in round 1. Add routing and taxonomy to the non-learnable surface, or justify why not.

**Fix:** make v1.0's exit demo the *value* story (shadow → assisted promotion with a measured MTTA or first-update delta) and move C14 to a post-1.0 research track.

### 3.8 "MTTR delta in shadow mode" is zero by construction
In shadow mode the agent **executes nothing** (CAP L244), so it can't change MTTA or MTTR. Any measured "delta" is noise. What shadow mode *can* measure is the **counterfactual**: *time from alert to agent proposal ready* vs *time from alert to the equivalent human action*, plus proposal correctness. Rename C22 accordingly. The real MTTA and MTTR delta is only measurable in assisted mode, ideally with a holdout of services left unassisted.

### 3.9 The allowlist and policy_grants are two sources of truth
The YAML allowlist (CAP §2) has `max_risk`, `requires_idempotency`, `approved_by`, `expires: 90d` and `scope`. The `policy_grants` table has `scope`, `granted_by` and `valid_to_ts`. Nothing says which is authoritative or how one becomes the other. If YAML is the source, then whoever can edit `supportagent.yaml` grants autonomy, and that is still unaudited (round-1 §6, "who changes supportagent.yaml?"). `approved_by` is still a free-text string, and `90d` still has no anchor.

**Fix:** grants live only in the DB, are created through the authenticated admin UI by an RBAC-authorized user, and anchor `valid_to_ts` to the grant time. YAML can *request* a grant, and a human approves it in the UI.

---

## 4. Scope and plan: the main unaddressed risk

### 4.1 v0.1 got bigger
Phase 5 (weeks 7–9, ROADMAP L190) now contains: the comms engine with plan-level approval, cadence and DLP; case memory with citations; feedback capture; redaction; bidirectional sync and takeover; storm grouping; RBAC with a two-person rule; value baselines; and read-only tools for logs, Airflow, cron, GitHub Actions, **a change feed (Argo CD/Terraform/Consul/CMDB), a feature-flag source and metrics/APM**. Each of those last three is a *family* of integrations, and every vendor needs its own auth, pagination, rate limits and result schema.

That is roughly **ten capabilities and 6–10 integrations in three weeks**. Even generously, Phase 5 alone is a quarter of work for two engineers.

The round-1 recommendation wasn't "add the missing things". It was "**cut v0.1, then add the missing things where they belong.**" The revision did the second half only.

### 4.2 v0.0's exit criteria depend on work scheduled after v0.0
CAP L257 says v0.0 (weeks 1–4) includes *ServiceNow, gate + outbox, C18, C21* and exits on **INV-01..05 green against the partner's real tickets**. In ROADMAP:
- Phase 1 (weeks 2–4) has only the **generic webhook** adapter;
- the gate, outbox and ServiceNow adapter are in **Phase 2 (weeks 4–6)**;
- RBAC (C21) is in **Phase 5**.

INV-01 and INV-04 test the outbox, so they can't pass at v0.0. The two documents disagree on what v0.0 contains.

### 4.3 The design-partner dependency starts in week 2
Real partner tickets are needed for Phase 1 (week 2). In practice, getting 6 months of incident data out of an enterprise (with legal review, NDA, DPA and a redacted export) takes **4–12 weeks**. That's good to name as the top risk, but the plan starts consuming the data before it could plausibly arrive. Start the data request on day 0, and give v0.0 a fallback of the partner's *live* shadow traffic, which usually clears legal faster than a historical export.

### 4.4 Team size is still unstated
Overlapping phases (Phase 2 in weeks 4–6, Phase 3 in weeks 5–6; Phases 7, 8 and 9 overlapping) imply parallel workstreams. State the team size. Every timeline judgment depends on it.

### 4.5 Recommended cut, revised
| Release | Contents | Defer |
|---|---|---|
| **v0.0 (wk 1–4)** | ServiceNow inbound (poll-based) + **service catalog** config, redaction *inside LLMClient*, dedup with DB unique index, classify, `work_note` action, gate + outbox with a trigger-enforced INV-01, trace/replay, invariants suite | everything else |
| **v0.1 (wk 5–12)** | Sync + takeover (with self-echo filter), plan-level comms with `comms_plans`, case memory, RCA-lite over **logs + GitHub Actions + one change source** the partner already has, storm grouping, RBAC (single approver role + no self-approval), shadow counterfactual metrics, quality suite on the real LLM | flags/APM integrations, two-person rule, DLP beyond field allowlists, cross-encoder rerank, backup with PITR |
| **v0.2** | Re-runs + grants UI, service requests (catalog mode), deep RCA, the second and third evidence sources, MCP | |
| **post-1.0** | C13/C14, multi-tenant operation, Zendesk | |

---

## 5. Remaining overclaims and soft spots

### 5.1 The entailment check is unspecified
`c.supported_by(evidence)` is now load-bearing for customer comms, but it has no mechanism (NLI model? LLM call? rule?), no failure default (does a timeout mean speculation?), no latency budget, and no eval measuring *its* accuracy. If it's an LLM call, it's another LLM-decided gate, which is fine, but then label it as such and default to `speculation` when it's absent.

### 5.2 The prompt cache key is too coarse
The cache key `model + prompt_hash + input_hash` (ROADMAP L115) misses:
- **assembled context**: retrieval results and case memory change as the KB grows, so the same input produces a different rendered prompt. Key on the **rendered-prompt hash**;
- **silent provider model updates** behind an alias. Pin dated model IDs and include the resolved model version in the key;
- **code changes to prompt assembly**. A template hash doesn't change when the code that fills it does.

### 5.3 The statistical gates still don't gate
- The quality suite is still **25–50 cases** (ROADMAP Phase 3). With N samples per case you reduce sampling noise, but not **coverage**. Fifty distinct scenarios can't represent the taxonomy.
- Shadow promotion at **≥80% over N≥30 "with a confidence band"**: at n=30, the 95% interval around 80% is about ±14 percentage points. Gate on the **lower bound** (for example, Wilson lower bound ≥ 0.75), or the band is decoration.
- The promotion metric is aggregate. A system that is 95% on P3/P4 and 40% on P1 passes. Gate per severity, P1/P2 especially.

### 5.4 Durability still assumes the source retries
ROADMAP L242: *"webhook losses covered by at-least-once redelivery"*. ServiceNow outbound REST messages and many alerting tools **don't retry by default**, or retry only briefly. With a poll-based inbound adapter and a cursor (`sys_updated_on > last_seen`), losses become structurally recoverable. Polling also helps §3.6, since it needs no customer-side business rule. *"Backup/restore + PITR tested in CI"* on SQLite needs a named mechanism (Litestream, for example). *"No thundering herd via the outbox QPS limiter"* conflates two problems: the QPS limiter governs outbound rate, not replica scheduling.

### 5.5 Still missing from round 1
- **Data retention and erasure.** `alerts.payload_ref` and `events.payload` hold raw, unredacted data indefinitely in an append-only store. There is no retention policy and no crypto-shredding.
- **A triage latency SLO.** With redaction, a rule-based classifier, N-sample production calls (or not?), entailment and rerank, what is the p95 from alert to first proposal? For a P1 it matters. Define the budget per stage.
- **An LLM spend breaker.** It's named in C20, but no component, config or event kind implements it.
- **Users/roles tables** for RBAC, and a **proposals** table. ARCH L107 and L223 reference `proposal.payload_hash`, but there's no `proposals` table. Approvals and proposals are still conflated.
- Webhook auth (HMAC, mTLS, replay window) belongs in the **normative** spec, not only in a ROADMAP phase line.

---

## 6. Documentation

- **Strip the review-response language from the spec.** "Was a real bug", "the earlier text was wrong", "(revised)", "now specified (was under-specified)", and "this fixes an unsupportable claim" belong in a `CHANGELOG.md` or a decisions log (ADRs). In a normative spec they cost credibility and make the doc longer.
- **Duplication grew.** CAP §4.1, §4.3 and §4.6 and ROADMAP §2.1–2.4 still restate ARCH designs in detail, with small differences (the CAP §4.6 pipeline, the ROADMAP §2.3 kind list). The rule "ARCH wins" doesn't stop drift. It just decides who loses the argument. Cut ROADMAP §2 down to links plus exit criteria, and CAP to the registry plus the tier table.
- **Fix the four dangling section references** (§3.1) by *writing* those sections: the pre-classifier and calibration, the prior parameter and evidence-source weighting, feedback capture, and the value baseline.
- Pitch language is still present ("money shot", "the marketing sentence", "the closer"). Move it to `PITCH.md`.
- The demo script contradicts itself. Step 1 proposes a comment, an email and an escalation, then step 2 *"rejects the ticket field-update"*, which was never proposed (ROADMAP L216).

---

## 7. Top 10 actions, in order

1. **Make INV-01 real**: a composite FK on `(approval_id, payload_hash)`, validity triggers for approval status and grant expiry/scope, `PRAGMA foreign_keys=ON`, and tests for rejected/expired/mismatched rows (§2.1).
2. **Bring customer comms under INV-01** with a `comms_plans` table, and restate the comms rule honestly (§2.2).
3. **Move redaction into `LLMClient`** so RCA, entailment, rerank and drafting are covered. Add an INV-06 secret-canary test (§2.4).
4. **Fix the `triage()` ordering** and make dedup a partial unique index with an upsert. Re-triage material changes **in place** (§2.5, §3.5).
5. **Replace `check:` strings with CEL/JMESPath**, add a **hypothesis taxonomy**, and add a **service catalog** for parameter binding (§2.3, §2.6, §2.7).
6. **Split `work_note` from `customer_comment`, and `enrich_page` from `create_page`.** Remove the dead comment-approval channel (§3.2, §3.3).
7. **Specify the takeover rules**: a self-echo filter, a narrow takeover definition, hand-back, and a poll-based sync cursor (§3.6, §5.4).
8. **Cut v0.1 for real** per §4.5. Reconcile the v0.0 contents between CAP and ROADMAP, and state team size (§4).
9. **Fix the learning-loop integrity issues**: time- and signature-disjoint holdout, routing and taxonomy made non-learnable, C14 out of the v1.0 headline (§3.7).
10. **Clean the docs**: write the four missing sections, move review-response text to a changelog, cut the duplicated designs from ROADMAP/CAP, and fix the demo script (§6).
