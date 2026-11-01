# SupportAgent — Technical Architecture (normative)

**This is the normative spec.** Where the three documents disagree, this one wins. [ROADMAP.md](ROADMAP.md) says *when* and *what ships*; [CAPABILITIES.md](CAPABILITIES.md) is the capability registry + tier definitions. This document fixes the **contracts, guarantees (as testable invariants), data model, and component behavior**. Schemas here are the source of truth.

> **Status: FROZEN for v0.0 build.** This document incorporates three adversarial design reviews. The invariants in §2 are the *only* guarantees we make, and each has a test ID. From here on, changes go through an **ADR attached to the code PR that implements them** ([`docs/adr/NNNN-*.md`](../adr/)), not through another document revision. The decision history lives in [CHANGELOG.md](../CHANGELOG.md), not in the spec body.
>
> **Provisional numbers:** every numeric threshold in this document (gate thresholds, confidence floors, SLO budgets, cadence defaults, promotion rates) is marked **(prov.)** where it appears. It is a starting value to be **re-set from v0.0 shadow data** on the design partner's real tickets, via an ADR — not a commitment made before data exists.

---

## 1. First principle

**The LLM decides *content*; code decides *control*.**

The LLM fills typed slots (classification label, severity, summary, hypothesis *evidence judgment*). Severity routes from the source record's priority when it has one; the LLM's severity is the fallback (ADR-0009). Code owns routing, tiers, budgets, approval, idempotency, and which evidence is admissible.

**Scope of the claim:** the LLM *does* pick the classification label and severity — both are inputs to routing. So the pathway is *indirectly* LLM-influenced. We do **not** claim "a bad prompt can never move the pathway." What we *do* claim, and enforce:

- The **mapping** label+severity → pathway is deterministic code (unit-tested per row).
- A **deterministic pre-classifier** (regex/heuristic on well-formed alerts) provides a second label; if it disagrees with the LLM, the case is routed to the **safe-default path**, not the LLM's path (§4.2).
- **Confidence calibration + a consistency check** gate low-confidence labels to safe-default.
- The **tier, the gate, and the idempotency key** are never LLM-decided.
- **Where the cross-check is absent** (free-text tickets the pre-classifier cannot read), the LLM label routes, but with a **severity cap**: it can never on its own route a case onto a P1/P2 pathway (§4.2).

---

## 2. The invariants (the *only* guarantees we make)

Each invariant has a **test ID** that must be green in CI. If a test can't be written, the invariant is not claimed.

| ID | Invariant | Enforced by | Tests |
|---|---|---|---|
| **INV-01** | No state-changing or customer-facing action executes unless its `outbox` row points at exactly one valid authorization: an **`approved` approval** (single-use, `payload_hash` matching the executed payload), an **approved, in-scope, unexpired policy grant** (never for customer-facing kinds), or an **approved comms plan** (customer-comms kinds only, template-bound). Authorization and payload columns are **immutable after insert**, and the executor re-hashes the payload before sending. | `proposals`/`approvals`/`policy_grants`/`comms_plans` FKs + **INSERT and UPDATE triggers** + `PRAGMA foreign_keys=ON` on every connection + executor re-hash (§6.2) | `test_gate_rejected_approval_blocked` · `test_gate_expired_approval_blocked` · `test_gate_payload_hash_mismatch_blocked` · `test_gate_grant_expired_or_out_of_scope_blocked` · `test_gate_grant_pending_not_enforceable` · `test_gate_approval_single_use` · `test_outbox_immutable_after_insert` · `test_executor_rehash_mismatch_aborts` · `test_customer_comm_never_by_policy_grant` · `test_comms_plan_revoked_blocked` · `test_plan_autosend_fields_from_record_state` · `test_self_approval_blocked` · `test_create_page_never_executable` · `test_trigger_kind_lists_match_code` |
| **INV-02** | While a case is unresolved, the same fingerprint (§4.1) maps to **one case**; duplicates increment `duplicate_count`; a severity escalation re-triages **in place**. Enforced by the schema, not by code order. | partial unique index on unresolved cases + `ON CONFLICT` upsert (§4.1) | `test_dedup_single_case` · `test_dedup_concurrent_no_second_case` · `test_dedup_human_owned_no_second_case` · `test_human_tickets_never_collapse` |
| **INV-03** | Every side effect, LLM call, tool call, and routing decision is an append-only `events` row with a `trace_id`. | `trace` (called before any stage returns) | `test_every_action_traced` |
| **INV-04** | Re-running any action or send with the same idempotency key is a **no-op** (at-least-once delivery, effectively-once *effect*). | `outbox`/`comms` unique keys + read-before-write reconciliation | `test_idempotent_replay` |
| **INV-05** | On any LLM/tool/config failure, the case ends in a **safe-default state** with zero unapproved side effects; the process does not crash. | `safe_default` per stage + timeouts | `test_fault_injection_suite` |
| **INV-06** | **No LLM-bound payload contains a detector-positive secret/PII token.** Redaction runs *inside* `LLMClient.structured()` — the only path to a provider — so no call site (classify, entailment, rerank, drafting) can skip it. | mandatory pre-send hook in `llm.py` + canary corpus | `test_llm_bound_canaries_absent` (canary tokens planted in fixture ticket bodies/log lines never appear in any captured provider request) |

**What we do *not* claim:** "structurally impossible" double-sends (it's *effectively*-once via reconciliation, see §6.2), "no exceptions" to approval (T1 policy-allowed actions exist, see CAP §2), that citations imply support (see §5.4), that an RCA outcome is a proven root cause (outcomes are *evidence-supported hypotheses*, §5), or that *all* PII never reaches a provider. INV-06 covers what the **detectors** flag. Detector **recall** on realistic secrets is a separately measured quality metric (§6.1), not a promise about the world.

**Engines:** every invariant test runs against **both SQLite and Postgres** in CI. The triggers in §6.2 are SQLite dialect; their PL/pgSQL twins live in `migrations/` and must pass the same test IDs. An engine that fails the invariant suite is not a supported engine.

---

## 3. Component overview

> **Modularity (ADR-0011).** The pipeline below is the **trust core**, and it stays fixed. Four plug-in layers attach to it: **skill packs** (stack-specific taxonomy, hypotheses, preclass rules, runbooks and prompt fragments, selected by the catalog's `stack:`), **tool bindings** (a capability bound to a first-party adapter or an MCP server), **connectors** (the incident system; ServiceNow is the first), and **knowledge sources** (docs as guidance, never evidence). Plug-ins add capability, never authority. Plan: [14-wp-modular.md](../tech-spec/14-wp-modular.md).

```
 inbound            core (the product)                                            outbound
 ──────────┬─────────────────────────────────────────────────────────────────────┴──────────
 adapters ─► dedup → [pre-classifier ‖ LLM classify] → routing → propose ─► [gate]
 (SN poll,       │              │                       │          │        │
  webhook)       │              └─► case lifecycle ─► human-owned? ─┤        │
                 │                       (sync events)             │   [outbox] ─► SN/email
                 │                                                  │   (all sends, incl. plan-authorized comms)
                 └─► storm grouping (case_groups) ─► [Loop B: RCA]  │
                                              │                     │
                     KB (runbooks YAML + prose) ─► retrieval ─► citations
                     case memory ─► feedback ─► learning (T3, caged, de-prioritized)
 cross-cutting: LLMClient (render → REDACT → provider; INV-06) · trace (INV-03)
                eval harness (INV-01..06) · RBAC · tenant isolation
```

Redaction is **not a pipeline stage**: it sits inside `LLMClient.structured()`, so it applies to every LLM call wherever it happens in the flow above (§6.1).

| Component | Responsibility | Enforces |
|---|---|---|
| `redact` | strip detector-positive PII/secrets **inside `LLMClient.structured()`** — every LLM call path | INV-06, §6.1 |
| `dedup` | fingerprint + unresolved-case uniqueness + storm grouping (`case_groups`) | INV-02 |
| `preclass` | deterministic label cross-check + calibration | §4.2 |
| `pipeline` (Loop A) | linear triage stages | INV-05 |
| `rca` (Loop B) | budgeted hypothesis–evidence | §5 |
| `routing` | label+severity → pathway (deterministic) | §4 |
| `gate` | approval state machine + RBAC | INV-01 |
| `outbox` | side-effect execution + reconciliation | INV-01/04 |
| `comms` | plan-level, cadence, DLP | INV-04 |
| `sync` | bidirectional ticket events → case lifecycle | §8 |
| `kb` | runbooks (YAML) + retrieval + graph | §5 |
| `memory` | case memory + feedback capture | §8.3 |
| `tools` | MCP + first-party (trust-classed) | §7 |
| `llm` | provider-agnostic structured calls | §9 |
| `trace` | append-only events, replay | INV-03 |
| `learning` | distill/evolve (T3, caged) | §10 |
| `evals` | invariant + quality suites | §2, §11 |

---

## 4. Loop A — Triage (linear)

**Dedup happens before the case exists as a decision, and it is a database property, not a lookup step.** The fingerprint is computed once and is the only dedup key in the system (§4.1).

```python
def triage(evt: AlertEvent) -> Case:
    fp = fingerprint(evt)                                 # §4.1 — the one definition
    case = cases.upsert(evt.tenant, fp)                   # INSERT … ON CONFLICT (§4.1); the SQL
                                                          # increments duplicate_count, not Python
    if not case.created:
        trace.decision("dedup", "linked", case.id)        # partial unique index (§15)
        if severity_escalated(evt, case):                 # the ONLY material change (§4.1)
            return retriage_in_place(case, evt)           # same case, new decision event — no 2nd case
        return case                                       # INV-02

    label  = preclass.classify(evt)                       # deterministic (§4.2); None = abstain
    llmres = llm.classify(evt, schema=TriageResult,       # redaction is inside
                          timeout=cfg.llm.timeout)        # LLMClient.structured() (§6.1)
    if llmres is None:
        return safe_default(case, reason="llm-failure")              # INV-05
    if label is not None and not prefix_match(label, llmres.classification):
        trace.decision("route", "conflict→safe_default",
                       why=f"preclass={label} llm={llmres.classification}")
        return safe_default(case, reason="classifier-conflict")      # §4.2
    if calibrated_score(llmres, label) < cfg.min_score:              # (prov.) — fail-closed
        return safe_default(case, reason="low-calibrated-score")
    severity = severity_strategy.resolve(evt, llmres.severity)       # ADR-0009: source priority, else LLM
    if label is None and severity in ("P1", "P2"):                   # no cross-check: severity cap
        trace.decision("route", "severity_cap",
                       why=f"{severity} without preclass agreement → human confirm")
        return safe_default(case, reason="unconfirmed-high-severity",
                            suggestion=llmres)                       # human sees the LLM's suggestion

    route = routing.lookup(llmres.classification, severity)          # row or GENERIC
    trace.decision("route", route.id, why=route.why)

    for a in [x for x in route.actions if tiers.allows(x, ctx)]:
        proposal = gate.submit(case, a)                               # INV-01, §6.2
    comms.plan(case, route.comms)                                     # comms_plans row, §6.3
    if route.rca and rca.should_run(llmres):
        schedule_rca(case, budget=route.rca)                          # → Loop B
    return case
```

**Per-stage failure behavior (INV-05):**

| Stage | Failure | Behavior |
|---|---|---|
| classify | timeout / garbage / schema fail / conflict | `safe_default`: human queue, zero actions, reason recorded |
| route | no table match | `default: generic` (`work_note` + RCA-lite) — never an exception |
| propose | partial set invalid | valid subset proceeds; invalid dropped **and logged** |
| gate | approver absent | proposal expires (TTL); case stays open; no execution |
| outbox | target 5xx | retry under same idempotency key; then safe-default + alert |

### 4.1 Fingerprint and dedup (INV-02 as a database property)

One definition, used by dedup, retrieval, and the graph:

```
alert-derived event:   fingerprint = normalize((source, resource, alert_type, error_signature))
human-filed ticket:    fingerprint = ("ticket", source, ticket_number)        -- identity, never merged
normalize() = strip IDs, UUIDs, timestamps, hex, IPs/ports, per-tenant constants
```

- **Alerts** dedup on what they *say*: the same source, resource, alert type and normalized error signature is the same problem.
- **Human-filed tickets** dedup on *who filed what*: a ticket's fingerprint includes its `number`. Two customers filing about the same service and category are **two cases**, never merged (`test_human_tickets_never_collapse`). The only duplicate of a ticket is a re-ingest of the same ticket. Relating human tickets to an alert-derived incident is **storm grouping** (§4.3), which links cases without merging them.

Dedup is enforced by the schema, so a race or a buggy code path cannot create a second case while one is unresolved:

```sql
-- One unresolved case per (tenant, fingerprint). Every non-terminal lifecycle state is covered —
-- including human_owned and reopened — so a duplicate never spawns a second case while a human works it.
CREATE UNIQUE INDEX ux_cases_unresolved
  ON cases(tenant_id, fingerprint)
  WHERE status <> 'resolved';

-- dedup is an upsert, not a find-then-insert. `created` is derived from the row itself
-- (a fresh insert has duplicate_count = 0); SQLite's changes() cannot report this.
INSERT INTO cases (id, tenant_id, fingerprint, status, duplicate_count, opened_ts, last_alert_ts)
VALUES (:id, :tenant, :fp, 'open', 0, :now, :now)
ON CONFLICT (tenant_id, fingerprint) WHERE status <> 'resolved'
DO UPDATE SET duplicate_count = duplicate_count + 1, last_alert_ts = :now
RETURNING id, (duplicate_count = 0) AS created;
-- Postgres twin: same statement; `created` may equivalently use (xmax = 0).
```

- **No time window.** A case absorbs matching alerts for as long as it is unresolved, however long that is. A resolved case falls out of the index, so the next matching alert opens a new case, linked to the old one by a `similar` edge (§5.3). There is no separate "1-hour window"; the case lifecycle *is* the window.
- **Material change = severity escalation only, re-triaged in place.** Severity (P3 → P1) is the only field that can change while the fingerprint stays the same. It re-runs classification and routing **on the same case row** and appends a `decision` event. A new `alert_type` or a new resource is, by construction, a *different fingerprint*, so it is a different case. If it belongs to the same outage, it is linked by storm grouping (§4.3), not merged.

### 4.2 Pre-classifier and calibration

- **The pre-classifier is deterministic code** (regex/field heuristics on well-formed alerts) and produces a label from the **same taxonomy** (§12) as the LLM. It is a cross-check, not a second brain.
- **Agreement is hierarchical:** `database/saturation` vs `database/leak` is agreement (prefix `database/`), not conflict. Exact mismatch *or* prefix mismatch → conflict. Free-text tickets that the rule set cannot read → **abstain** (no label), which never triggers conflict.
- **Conflict → safe-default is kept on purpose (INV-05):** a disagreement means we do not know, so we route to a human. The failure mode this prevents (silent LLM misroute) is worse than the cost (a human queue slot).
- **Calibration is measured, because conflict volume is a real risk:** every preclass/LLM pair is recorded (`preclass` + `classify` events); the **conflict rate** and **abstention rate** are first-class metrics in `stats`, reported per taxonomy prefix. If conflict rate is high, the fix is a sharper rule set or a taxonomy split — both reviewed PRs — not weakening the conflict rule.
- **LLM confidence is self-reported and is not trusted.** The LLM's own `confidence` field is logged for observability only and never gates a decision.
- **`calibrated_score(llmres, label)` is defined, and it fails closed.** It is the **empirical accuracy of the LLM's label** on the eval holdout, taken from a per-label table produced by the quality suite (§11). When the pre-classifier agrees, the "with agreement" row is used, otherwise the "abstain" row. A label with **no table entry** (new, or too few holdout cases) scores **0**, which is below any `min_score`, so the case goes to the safe default. There is no "floor at the threshold" rule. Absence of evidence never passes.
- **Free-text tickets (pre-classifier abstains) are routed, with a severity cap.** Abstention is not conflict, so the LLM label routes P3/P4 cases normally if its calibrated score clears `min_score` (prov.). An LLM-only **P1/P2** is never auto-routed: it goes to the human queue *with the LLM's suggestion attached* (`unconfirmed-high-severity`). The highest-cost misroutes therefore always get a human look, and the product still works on the bulk of free-text volume.

### 4.3 Storm grouping (C20) — linking, not merging

A storm is many **distinct** fingerprints at once (a cascade across resources), not many duplicates. Grouping is deterministic, LLM-free, and links cases under a parent without merging them:

```sql
CREATE TABLE case_groups (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
  opened_ts INTEGER NOT NULL, closed_ts INTEGER,
  rule TEXT NOT NULL,                 -- which grouping rule fired (below)
  member_count INTEGER NOT NULL DEFAULT 0
);
-- cases.group_id → case_groups.id (nullable)
```

**Grouping rule (prov.)**: a new case joins an open group when, within a 10-minute (prov.) window, it shares **any** of: (a) a service in the service catalog (§7), (b) a catalog **dependency edge** with a group member (`services.depends_on`), or (c) the same upstream `source` as at least 5 (prov.) other new cases. Storm mode opens when a tenant sees more than 20 (prov.) new cases in 5 minutes. In storm mode:

- members get dedup + grouping + **one** triage of the group (the first member, or the highest-severity one) rather than N LLM classifications. Other members inherit the group's route as a *suggestion* and are safe-defaulted to the human queue under the group;
- RCA runs **once per group**, not per member;
- comms planning happens at group level, with one comms plan per affected audience;
- the LLM spend breaker (§11) is evaluated per tenant regardless.

Grouping is evaluated by the 1,000-alert storm soak (§19): 0 duplicate cases, bounded LLM calls (≤ number of groups + configured headroom), 0 double-sends.

---

## 5. Loop B — RCA (budgeted hypothesis–evidence)

> **ADR-0011:** the hypotheses, probes and runbooks come from the case's active skill packs (`common` plus the service's stack packs). Retrieved docs may reorder hypotheses and suggest checks, but SUPPORTED still requires tool evidence.

The only free-running loop. It is **one algorithm**: a *plan* of steps, executed in order, each step testing one declared hypothesis. Where a runbook matches, the runbook **is** the plan. Where none matches, the LLM may propose hypotheses — but **only IDs from the fixed hypothesis taxonomy** (§12), each paired with a **registered probe** (tool + args + `check`); a proposed hypothesis with no registered probe is skipped and logged, never invented. Termination and evidence are **machine-checkable** (CEL predicates, CI-compiled), not an LLM judging prose.

```python
def rca(case: Case, budget: Budget) -> RCAArtifact:
    rb = kb.match(case)                                     # runbook (YAML, §5.1) or None
    if rb is not None:
        plan = [(s.hypothesis, s) for s in rb.steps]        # the runbook IS the plan, in order
    else:
        proposed = llm.propose_hypotheses(case,
                    ids=HYP_TAXONOMY.ids, max_=budget.hyps, schema=HypothesisSet)
        plan = [(h, PROBES[h]) for h in proposed if h in PROBES]   # registered probes only
        if not plan:
            trace.decision("rca", "no_runbook_no_probes")
            return RCAArtifact(outcome="INCONCLUSIVE", reason="no testable plan")

    evidence: list[Evidence] = []
    for h, step in plan:
        if budget.exhausted():  trace.decision("budget", "stop"); break
        ev   = tools.run(step.tool, step.args.bind(case.service), trace=case.trace)  # bound via service catalog (§7)
        ok, why = eval_cel(step.check, result=ev, service=case.service, case=case)  # CEL over typed tool output
        evidence.append(Evidence(h, ev, ok, why))
        if ok and step.stop_if:  break

    outcome = reduce(evidence)                 # truth table below: categorical, evidence-only
    conclusion = conclude(evidence, outcome)   # only from evidence-linked claims
    for c in conclusion.claims:
        verdict = entail(c, c.cited(evidence))          # §5.4: supports | contradicts | unknown
        if verdict != "supports":  c.tag("speculation") # excluded from every send, internal drafts only
        if verdict == "contradicts":  c.tag("contradicted")  # surfaced to the human, never sent
    return RCAArtifact(hyps=[e.h for e in evidence], evidence=evidence,
                       outcome=outcome, conclusion=conclusion, score=score(evidence))
```

**`reduce()` — defined as a truth table over step results only.** Entailment runs *after* `reduce()` and acts on the **claims** in the conclusion, never on the outcome:

| Condition (evaluated in this order) | Outcome |
|---|---|
| ≥ 2 steps with `ok=true` whose hypothesis IDs `excludes` each other (§12 taxonomy) | **INCONCLUSIVE** (conflicting signals; both recorded) |
| ≥ 1 step with `ok=true` (no declared conflict) | **SUPPORTED**: the first such step in plan order |
| Plan completed, all steps ran successfully, all `ok=false` | **CONSISTENT_UNCONFIRMED** |
| Anything else (budget exhausted, a tool call failed, plan incomplete) | **INCONCLUSIVE** (reason recorded) |

- **Why `SUPPORTED`, not "confirmed":** a passing `check` shows that evidence *consistent with* a hypothesis exists, for example "a run in this service's repos failed in the window". That is correlation, not proof of cause. The outcome name says only what the evidence shows.
- **RCA outcomes never drive customer comms in v0.x.** A runbook may declare `comms_if_supported`, but only with `audience: internal`; schema validation rejects `customer`. Customer-facing statements about cause are always written or edited by a human under a per-send approval (§6.3).
- **Budgets (prov.):** lite ≤ 5 tool calls / ≤ 3k tokens / ≤ 3 hypotheses; deep ≤ 25 calls (config). Exhaustion is a **logged, valid** outcome.
- **Outcome is categorical**, not a scalar `0.82`. `score` is a derived, labeled value for reporting, never the decision variable.
- **`check:` is CEL, not code.** Expressions are compiled in CI against the tool's declared result schema and the bound `service`/`case` fields; the runtime evaluates compiled expressions only. Arbitrary Python strings are never executed, so a poisoned runbook or MCP-declared tool schema cannot inject code through `check:`.
- **Citation faithfulness (§5.4):** a claim is eligible for any send only if the entailment check returns `supports`, not merely because "a citation exists".
- **Evidence sources must match the prior (§5.5).** A hypothesis we cannot test with a registered tool can never be SUPPORTED. That is what keeps the prior honest.

### 5.1 Runbooks — executable YAML, not frontmatter prose

Runbooks are **`runbooks/*.yaml` validated against a JSON Schema in CI**, with a sibling `*.md` for humans (Obsidian-friendly). This fixes: fragile frontmatter, hardcoded `args` (now **typed, bound from the case** at runtime), and prose `expected_evidence` (now a machine predicate).

```yaml
# runbooks/db-pool.yaml  (validated by CI against runbook.schema.json)
id: runbook:db-pool
applies_when:
  classification: database/saturation
  signals: [pool_exhaustion, conn_wait_gt_5s]
budget: {max_tool_calls: 5, max_tokens: 3000}
steps:
  - id: s1
    hypothesis: recent_binary_or_config_push        # ID from the fixed taxonomy (§12)
    tool: github_actions.recent_runs
    args: {window: 24h, repos: "$service.repos"}       # bound at runtime from the service catalog row (§7)
    check: "exists(result.runs, r, r.repo in service.repos && r.state in ['failed','completed-with-errors'] && r.finished_ts <= case.opened_ts)"
    stop_if: true
  - id: s2
    hypothesis: capacity_decay
    tool: logs.grep
    args: {pattern: "pool exhaustion|connection timed out", paths: "$service.log_paths"}
    check: "size(result.matches) > 0"
comms_if_supported: {audience: [internal], template: db-pool-internal}  # internal only in v0.x (§5)
```

**Binding is runtime, via the catalog.** A runbook names a *service field* (`$service.repos`, `$service.log_paths`, `$service.dags`), never a free string from the case. At run time the case's service (resolved from the catalog, §7) supplies the values, and the same `service` object is visible to the CEL `check`. CI validates that every `$service.*` reference names a real catalog field.

**Following a procedure is mechanical and traced.** Loop B iterates `steps[]` honoring `check` (CEL, CI-compiled) and `stop_if`. That is the upgrade from "RAG over docs".

### 5.2 Retrieval — specified

- **Error-signature normalization** (the `normalize()` of §4.1) is the similarity engine: the normalized **error signature** — a component of the fingerprint, present only on alert-derived events — powers `similar` edges and case-memory retrieval; the fingerprint itself powers dedup. One `normalize()`, two uses, one definition.
- **Ranking** (all weights are config, all components defined):
  - `bm25` over **normalized** text (FTS5) — BM25 on short alert text is weak alone, hence:
  - `signature_match` — exact/partial normalized error-signature equality (the strongest signal);
  - `structured_match` — service / classification / severity frontmatter;
  - `graph_similarity` — **defined** as: cosine of the node's `resolved_by` + `similar` neighbor sets (shared fingerprint or shared resolution), not a vague "walk";
  - `recency`.
- **Rerank:** top-10 → cross-encoder (local, provider-agnostic) or a bounded LLM-judge slot (logged) → top-3 **with citation IDs**.
- **Honesty:** zero matches → return nothing. We never fabricate a "similar case."

### 5.3 Graph (derived, never hand-maintained)

```
nodes(id, tenant_id, kind, ref, label, updated_at)
  kind: case | runbook | playbook | service | tool | action | signature
edges(src, dst, tenant_id, kind, weight, created_at)
  kind: cites | resolved_by | similar | uses_tool | proposed_by | shares_change
```
`similar` edges are derived from normalized-signature equality (§5.2), not asserted. Powers: case memory, provenance chains, correlation (C3), and distillation input.

### 5.4 Citation faithfulness — specified

Tagging unlinked claims as `speculation` only checks that a citation **exists**. The added rule: a claim is customer-comms-eligible only if an **entailment check** confirms the cited evidence actually supports it.

**Mechanism (bounded, fail-closed):**

- Slot: `entail(claim, cited_evidence) → supports | contradicts | unknown` — a bounded LLM-NLI call through `LLMClient.structured()` (so INV-06 redaction applies), with both sides logged into the trace.
- **Failure = `unknown`, never `supports`:** timeout, schema failure, or provider error → the claim is tagged `speculation` and excluded from every send.
- **Latency budget (prov.):** ≤ 5 claims per RCA artifact and an overall entailment budget of 20 s (prov.) per artifact (config). Over budget, the remaining claims are `speculation`. The per-claim latency depends on the provider. A local Ollama model will be slower, which only means more claims fall to `speculation`, never that a claim passes.
- **Its own eval metric:** citation-faithfulness rate in the quality suite (§11), measured on curated claim/evidence pairs, so the checker itself is regression-tested.
- **`contradicts` acts on the claim, not the outcome.** Entailment runs after `reduce()` (§5). A `contradicts` verdict tags the claim `contradicted` and shows it to the human on the case view. It does not change the categorical outcome, which is computed only from step results.

### 5.5 Prior weights and evidence-source gating — defined

The change-induced prior (Google SRE App C ≈ 68% of outages change-induced — cited as a fact, text not redistributed) is a **per-deployment parameter**, not a model belief:

- **`cfg.prior`** is an explicit weight table over the hypothesis taxonomy (§12), e.g. `recent_change_push: 0.35`. It affects **ordering only** — which hypothesis is tested first when no runbook matches. It never gates, never executes, and is versioned in config like any other control plane row.
- **Evidence-source gating (the honesty rule):** a hypothesis can reach **SUPPORTED** only via a step whose tool is **registered**. No change feed registered ⇒ `recent_change_push` can at best be `INCONCLUSIVE`. This is what makes the prior falsifiable: we do not *claim* the prior's ranking unless we have the source to test it.
- **Maintenance windows / change freezes:** when a freeze is active (read from the change feed or a `freeze` config flag), `recent_change_push` weight is zeroed for the window and the zeroing is logged (`prior_zeroed` event).
- **Retrieval weights** (`cfg.retrieval.weights` over the §5.2 components) affect **injection order only** — never admissibility of evidence. Admissibility is the step's CEL `check` + the entailment check (§5.4).
- **Re-ranking** (§5.2) is a bounded, logged slot (local cross-encoder or LLM-judge); it orders candidates, it does not create them.

---

## 6. Gate, outbox, and comms — real enforcement

### 6.1 Redaction — inside the LLM client (INV-06)

Redaction is **not a stage you call before an LLM call**; it lives **inside `LLMClient.structured()`** — the single code path that reaches a provider. `classify`, entailment (§5.4), rerank, and comms drafting all go through it, so no call site can skip it and a new LLM slot cannot be added without passing through the same function:

```
structured(prompt, schema, ...) → render → REDACT (mandatory) → provider
```

- **Detector battery** (configurable per tenant): emails, API keys/tokens, connection strings, account/card numbers, phone numbers, custom patterns. Matches are replaced with **typed placeholders** (`<EMAIL_1>`); the placeholder map is held **out of the trace payload** (the trace itself must not become a leak channel).
- **Canary suite (CI, hard gate):** known synthetic secrets, one per detector class, are planted in fixture ticket bodies and log lines; a provider-capture mock asserts none of them appear in any outbound request. This is `test_llm_bound_canaries_absent` (INV-06). It proves the redaction path is **wired in** for every call site. It does *not* prove the detectors are good, because canaries built from the detector patterns match by construction.
- **Detector recall (quality metric, not an invariant):** a **held-out corpus of realistic secrets** is maintained separately from the detector code and never shown to detector authors. It includes keys embedded in URLs, base64-wrapped tokens, JDBC/ODBC strings with inline passwords, cloud credentials split across log lines, and PII in free prose. Recall per class is reported in the quality suite (§11) and tracked over time. A recall drop blocks the release in the same way a quality regression does.
- **Honest scope:** INV-06 proves that *detector-flagged* secrets/PII don't reach providers. How much the detectors flag is the recall metric above. We do not claim "no PII ever" in the open-world sense.
- The **inbound ticket body is the primary prompt-injection surface** — it is treated as untrusted data in every stage; injection canaries on inbound cases are part of the fault-injection suite (§19).

### 6.2 Approval + outbox — INV-01 as database constraints

A `CHECK (x IS NOT NULL)` is not INV-01, because it cannot see an approval's **status**, its **payload hash**, or a grant's **validity**. INV-01 is enforced by foreign keys, an INSERT trigger that re-checks the *semantics*, an UPDATE trigger that makes authorization immutable, and an executor that re-hashes before sending.

```sql
-- SQLite: foreign keys are OFF by default → ON per connection, in engine bootstrap
-- (an engine-connect hook; test_fk_pragma_on_every_connection asserts it):
PRAGMA foreign_keys = ON;

CREATE TABLE proposals (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  case_id TEXT NOT NULL REFERENCES cases(id),
  action_kind TEXT NOT NULL,                    -- canonical enum, §12.2
  scope TEXT NOT NULL,                          -- literal scope string, §6.4
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,                   -- sha256(canonical_json(payload_json))
  trace_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed','pending_approval','approved','rejected','expired','executed','superseded')),
  created_by TEXT NOT NULL,                     -- 'agent' | users.id
  created_at_ts INTEGER NOT NULL
);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  proposal_id TEXT NOT NULL REFERENCES proposals(id),
  approver_id TEXT NOT NULL REFERENCES users(id),   -- identity from OIDC, never free text
  status TEXT NOT NULL CHECK (status IN ('approved','rejected')),
  payload_hash TEXT NOT NULL,                  -- the hash the approver actually saw
  reason TEXT,
  approved_at_ts INTEGER NOT NULL
);

-- No self-approval (enforced, not documented):
CREATE TRIGGER trg_no_self_approval
BEFORE INSERT ON approvals
WHEN EXISTS (SELECT 1 FROM proposals p
             WHERE p.id = NEW.proposal_id AND p.created_by = NEW.approver_id)
BEGIN SELECT RAISE(ABORT, 'INV-01: self-approval'); END;

CREATE TABLE policy_grants (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  action_kind TEXT NOT NULL,
  scope TEXT NOT NULL,                         -- LITERAL, exact-match (no globs; §6.4)
  status TEXT NOT NULL DEFAULT 'requested'
    CHECK (status IN ('requested','approved','rejected','revoked')),
  requested_by TEXT NOT NULL,                  -- users.id | 'config:<sha>' (from `grants request`)
  requested_at_ts INTEGER NOT NULL,
  granted_by TEXT REFERENCES users(id),        -- NULL until approved
  granted_at_ts INTEGER,                       -- NULL until approved
  valid_to_ts INTEGER,                         -- NULL until approved; anchored at approval time
  CHECK (status <> 'approved' OR (granted_by IS NOT NULL AND granted_at_ts IS NOT NULL
                                  AND valid_to_ts IS NOT NULL AND granted_by <> requested_by))
);

CREATE TABLE outbox (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  action_kind TEXT NOT NULL,
  scope TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','done','failed')),
  approval_id TEXT UNIQUE REFERENCES approvals(id),       -- approval path: SINGLE-USE (UNIQUE)
  policy_grant_id TEXT REFERENCES policy_grants(id),      -- policy path (T1/T2 non-customer kinds)
  comms_plan_id TEXT REFERENCES comms_plans(id),          -- plan path (customer-comms kinds, §6.3)
  claimed_at_ts INTEGER, done_at_ts INTEGER,
  CHECK ((approval_id IS NOT NULL) + (policy_grant_id IS NOT NULL) + (comms_plan_id IS NOT NULL) = 1)
);

-- INV-01, the semantic layer (INSERT):
CREATE TRIGGER trg_outbox_inv01
BEFORE INSERT ON outbox
WHEN
  -- (0) kinds that are never executable in v0.x, by any path:
  NEW.action_kind IN ('create_page')
  OR
  -- (1) approval path: approved, single-use, same payload the approver saw
  (NEW.approval_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM approvals a JOIN proposals p ON p.id = a.proposal_id
      WHERE a.id = NEW.approval_id
        AND a.status = 'approved'
        AND p.status = 'approved'                       -- NOT 'executed': an approval authorizes once
        AND a.payload_hash = NEW.payload_hash
        AND p.payload_hash = NEW.payload_hash           -- approval, proposal and outbox all agree
        AND p.action_kind = NEW.action_kind
        AND p.tenant_id   = NEW.tenant_id
  ))
  OR
  -- (2) policy path: approved, unexpired, exact scope, NEVER a customer-facing kind
  (NEW.policy_grant_id IS NOT NULL AND (
      NEW.action_kind IN ('customer_comment','send_email_customer','create_ticket_customer',
                          'watch_list_add_customer')            -- CUSTOMER_FACING_KINDS, §12.2
      OR NOT EXISTS (
        SELECT 1 FROM policy_grants g
        WHERE g.id = NEW.policy_grant_id
          AND g.tenant_id = NEW.tenant_id
          AND g.status = 'approved'                     -- a 'requested' row is never enforceable
          AND g.valid_to_ts > CAST(strftime('%s','now') AS INTEGER)
          AND g.action_kind = NEW.action_kind
          AND g.scope = NEW.scope)))                    -- literal equality, no pattern matching
  OR
  -- (3) plan path: approved plan, customer-comms kinds only, template-bound
  (NEW.comms_plan_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM comms_plans cp JOIN approvals a ON a.id = cp.approval_id
      WHERE cp.id = NEW.comms_plan_id
        AND cp.status = 'approved'                      -- not revoked / satisfied
        AND a.status = 'approved'
        AND a.payload_hash = cp.plan_hash               -- the plan the approver saw
        AND cp.tenant_id = NEW.tenant_id
        AND NEW.action_kind IN ('customer_comment','send_email_customer')
        AND json_extract(NEW.payload_json,'$.template_id') = cp.template_id))
BEGIN
  SELECT RAISE(ABORT, 'INV-01: no valid authorization');
END;

-- Single-use: the approved proposal flips to 'executed' in the same transaction as the insert.
CREATE TRIGGER trg_outbox_mark_executed
AFTER INSERT ON outbox WHEN NEW.approval_id IS NOT NULL
BEGIN
  UPDATE proposals SET status = 'executed'
  WHERE id = (SELECT proposal_id FROM approvals WHERE id = NEW.approval_id);
END;

-- INV-01, the immutability layer (UPDATE): only status/timestamps may change after insert.
CREATE TRIGGER trg_outbox_immutable
BEFORE UPDATE OF tenant_id, case_id, action_kind, scope, payload_json, payload_hash,
                 idempotency_key, approval_id, policy_grant_id, comms_plan_id ON outbox
BEGIN
  SELECT RAISE(ABORT, 'INV-01: outbox authorization/payload is immutable');
END;
```

- **The forbidden-kind lists are literals in the trigger, not a table.** A table is data, and any DB writer could `DELETE` from it. The literal lists are **generated at migration time** from the Python constants `CUSTOMER_FACING_KINDS` and `NEVER_EXECUTABLE_KINDS` (§12.2), and `test_trigger_kind_lists_match_code` fails CI if they drift. Changing them needs a migration, which needs a reviewed PR.
- **The executor re-hashes before every send.** After claiming a row, the executor computes `sha256(canonical_json(payload_json))` and aborts (`status = 'failed'`, `executor_hash_mismatch` event) if it differs from `payload_hash`. The trigger guarantees the hash matches the approval. The re-hash guarantees the bytes sent match the hash (`test_executor_rehash_mismatch_aborts`).
- **State machine:** `proposed → pending_approval → approved | rejected | expired`, then `approved → executed` (single use, set by `trg_outbox_mark_executed`). Approving a stale or tampered proposal is **rejected by the trigger**, not discouraged.
- **INV-01 tests (CI, all must be green, both engines):** `test_gate_rejected_approval_blocked` · `test_gate_expired_approval_blocked` · `test_gate_payload_hash_mismatch_blocked` · `test_gate_grant_expired_or_out_of_scope_blocked` · `test_gate_grant_pending_not_enforceable` · `test_gate_approval_single_use` (second outbox row on an executed approval → ABORT) · `test_outbox_immutable_after_insert` · `test_executor_rehash_mismatch_aborts` · `test_customer_comm_never_by_policy_grant` (a customer-facing kind with a valid grant → ABORT) · `test_comms_plan_revoked_blocked` · `test_self_approval_blocked` · `test_create_page_never_executable`.
- **Idempotency (INV-04):** `outbox.idempotency_key` is `UNIQUE`, and **every external effect, comms included, is an outbox row** (§6.3). SQLite has no `SELECT … FOR UPDATE`; claims are `BEGIN IMMEDIATE` plus a status compare-and-swap (`UPDATE … SET status='claimed' WHERE id=? AND status='pending'`). The immutability trigger allows this, because `status` is not a protected column.
- **The honest guarantee is *effectively-once*, not *structurally-once*.** There is a crash window between "external send succeeded" and "we marked it sent", and ServiceNow and email don't accept our idempotency key. So we do **read-before-write reconciliation**: before any send, query the target for a record carrying our stable `idempotency_key` (embedded as a comment marker or field), and no-op if it is present. We claim at-least-once delivery plus an effectively-once *effect*, and we **measure** double-sends in the soak (target 0).

### 6.3 Comms — plans are approved proposals, sends are outbox rows

Per-update approval of every 4-hourly customer email is the approval-fatigue trap. Plan-level approval avoids it, but only if the plan goes through the same approval machinery as everything else and every send goes through the same outbox.

```sql
CREATE TABLE comms_plans (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  case_id TEXT NOT NULL REFERENCES cases(id),     -- or a case_group's lead case (§4.3)
  audience TEXT NOT NULL CHECK (audience IN ('customer','internal')),
  channel TEXT NOT NULL,
  template_id TEXT NOT NULL,
  allowed_fields_json TEXT NOT NULL,              -- the fields that may vary, each with its SOURCE (below)
  cadence_minutes INTEGER,
  plan_hash TEXT NOT NULL,                        -- sha256 over {audience, channel, template_id,
                                                  --   allowed_fields_json, cadence_minutes}
  proposal_id TEXT NOT NULL REFERENCES proposals(id),  -- the plan is a proposal (kind 'comms_plan')
  approval_id TEXT REFERENCES approvals(id),      -- NULL until approved; RBAC + no-self-approval apply
  status TEXT NOT NULL DEFAULT 'pending_approval'
    CHECK (status IN ('pending_approval','approved','rejected','revoked','satisfied'))
);

CREATE TABLE comms (                              -- the comms HISTORY / rendering record
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  kind TEXT NOT NULL,                             -- work_note | customer_comment | send_email_customer
  plan_id TEXT REFERENCES comms_plans(id),
  outbox_id TEXT NOT NULL UNIQUE REFERENCES outbox(id),  -- every send IS an outbox row (INV-01/04)
  body_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sent','failed')),
  sent_at_ts INTEGER
);
```

- **There is one send path.** Every send, internal or customer, plan-authorized or per-send-approved, is an `outbox` row, so INV-01 (§6.2) and INV-04 cover all of them. `comms` is a history and rendering record keyed to its `outbox_id`. It cannot send anything by itself.
- **Plan approval is a real approval.** A plan is a proposal of kind `comms_plan`, approved through `approvals` like any other proposal, with RBAC, no-self-approval and `payload_hash = plan_hash`. The trigger's plan path re-checks that hash on every send, so editing a plan after approval (template, fields, cadence) invalidates it.
- **Auto-send rule.** Under an approved plan, a rendered update auto-sends only when (a) the template is the plan's template, and (b) every varying field is in `allowed_fields_json` **and its value comes from the declared source**. Each allowed field declares `source: record_state`, meaning it is read from the ServiceNow record (`state`, `priority`, `assigned_to.name`, `work_notes`-free fields) **as last set by a human actor** (§8.2; when a human created the record, its values at creation count, ADR-0010), or `source: schedule` (`next_update_eta`, computed from cadence). **No allowed field may have `source: agent`**. A value the agent inferred, such as an LLM saying the incident is resolved, can never auto-send to a customer (`test_plan_autosend_fields_from_record_state`). Telling a customer "resolved" is the most consequential thing the system can say, so it only repeats what a human set.
- **Free text always needs per-send approval.** Any customer-facing body that contains text outside the template's allowlisted fields goes through a per-send `approval_id`.
- **Customer comms are never grant-authorized (hard, not configurable).** The trigger's policy path rejects every customer-facing kind (§6.2, `test_customer_comm_never_by_policy_grant`). Customer-facing communication is a human decision: a human-approved plan with human-sourced field values, or a human-approved message.
- **Internal and customer notes are different actions.** `work_note` is an internal work note on the ServiceNow record (the `work_notes` journal field, **not caller-visible**): T1, policy-allowable by default (§6.4). `customer_comment` is a caller-visible comment (the `comments` journal field): T2, never grant-authorized. `doctor` verifies the field mapping on the partner's instance (that `work_notes` is not exposed to the caller via portal configuration).
- **Outbound DLP (v0.1 scope):** customer-facing templates render **only allowlisted fields**. LLM free text goes out **only as a human-edited draft** (per-send approval), never raw. Pattern-based DLP beyond the field allowlist is v0.2.
- **Approval fatigue is measured** (median approve time, percentage of one-click rubber-stamps). If it degrades, we know.

### 6.4 RBAC — identities are rows, grants live in the DB

- **`users` and roles are tables** (§15): `users.id` is the stable OIDC `sub`; `role_assignments(user_id, tenant_id, role, scope)` with `role ∈ {viewer, analyst, approver, admin}`. Every approver, granted-by and requested-by column is an FK to `users.id` (or a `config:<sha>` request marker), and there are **no free-text names anywhere in the enforcement path**. OIDC login ships in **v0.0**, because v0.0's approvals already need real identities.
- **Grants are DB-only and authoritative, and their lifecycle is in the schema.** `supportagent grants request --from config` writes `policy_grants` rows with `status='requested'` and `requested_by='config:<sha>'`. Those rows are **not enforceable**, because the trigger requires `status='approved'`. An `admin` approves a row in the UI, which sets `status`, `granted_by`, `granted_at_ts`, and `valid_to_ts` (anchored at approval time). The table CHECK requires `granted_by <> requested_by`. **An edit to YAML never directly creates an enforceable row.**
- **Scopes are literal strings, exact-match only.** `scope` names one concrete target, for example `airflow:dag:nightly-sync` or `gha:repo:acme/prod-api:workflow:deploy.yml:rerun_failed_only`. There is **no glob or pattern matching anywhere in the enforcement path**: the trigger compares with `=`. A YAML request containing a pattern is **expanded at request time** against the service catalog into one literal row per concrete target, each approved individually or in a reviewed batch. A new target therefore needs a new grant, which is the point.
- **First-run setup: default T1 grants are customer-approved, not vendor-seeded.** "T1 policy-allowed by default" means that `supportagent doctor --setup` writes `requested` grants for the default T1 kinds (`work_note`, `enrich_page`, `watch_list_add_internal`), scoped to the catalog's services: one literal `service:<name>:<kind>` row per service and kind, which is also the scope T1 proposals carry ([ADR-0005](../adr/0005-t1-grant-scope-per-service.md)). The customer's first admin approves them in a single setup screen. Until then T1 actions queue as proposals like anything else, so no grant exists that a customer human did not approve.
- **No self-approval** is enforced by `trg_no_self_approval` (§6.2) for proposals and by the `granted_by <> requested_by` CHECK for grants (`test_self_approval_blocked`). It ships in v0.0.
- **Two-person rule** for high-risk kinds (`job_rerun`, `create_ticket_customer`): two distinct `approver_id` rows are required. This is **v0.2** (C21 full).
- **A ticket comment is never an approval channel.** A customer commenting on their own ticket would otherwise be a privilege-escalation path. Approvals go through the authenticated admin surface only, bound to the exact `payload_hash`.

---

## 7. Tools

> **ADR-0011:** packs name tool **capabilities** with one result schema each. A deployment binds every capability to a first-party adapter or an MCP tool, under the WP-204 registration contract. An unbound capability is skipped.

- **First-party (in-process):** `logs`, `airflow`, `github_actions`, `servicenow`, each registered with a **trust class** (`read` / `execute` / `state-changing`) and a tier; `state-changing` ⇒ T2 ⇒ gate. **`cron` re-invoke is a code-execution capability** and is deferred until a constrained runner is specified.
- **Service catalog (v0.0 config artifact, required):** one `services` row per service, `{name, repos, log_paths, dags, cmdb_ci, oncall_schedule, owners, depends_on}`. It is what makes runbook binding *real*: `$service.repos` resolves to **the service's registered repos**, not whatever string the ticket body contained (§5.1). A case's service is resolved from the alert's `resource` or the ticket's CMDB CI **through the catalog**, and an unresolvable service gets the safe default. The catalog also feeds the **action-target injection validator**: every state-changing action's target must resolve to a catalog entry bound to the case's service, or the action is **dropped and logged**. The LLM cannot invent action targets, and a poisoned ticket body cannot redirect one. `depends_on` feeds storm grouping (§4.3).
- **Inbound transport security:** inbound webhooks (the generic webhook adapter) require **HMAC signature verification (or mTLS)**, **replay protection** (timestamp + nonce, 5-minute window), and a source allowlist. Rejections are logged as `webhook_rejected` audit events. ServiceNow is **poll-based (§8.1)**, so it has no webhook path.
- **Evidence sources, staged:**
  - **v0.1:** logs, Airflow, GitHub Actions, ServiceNow, plus **exactly one change source the design partner already has** (one of: Argo CD, Terraform Cloud runs, Consul, or ServiceNow change records, chosen at onboarding; CAP §6 item 7).
  - **v0.2:** the feature-flag source and metrics/APM (Prometheus / Datadog / CloudWatch, read-only), as partner needs dictate.
  - The prior honesty rule (§5.5) handles the gap: without a flag source, `recent_change_push` via flag flips cannot be SUPPORTED, only INCONCLUSIVE.
- **MCP extension (WP-M06, ADR-0012):** a minimal synchronous streamable-HTTP client, conformance-tested against the official SDK's server; registration is config (endpoint, declared tools, class and tier); undeclared tools are denied; read-only registrations only until the action gate covers MCP. **Tool output is data, never instructions.** The **primary injection surface is the inbound ticket body**, not MCP tools. Defences: inbound text treated as untrusted, injection canaries on inbound cases, and the catalog-based action-target validator above.
- **Load-time validation:** a tool declared `class: read` at `tier: T2`, or `state-changing` at `tier: T0`, **fails config validation** (trust class and tier must agree).
- **ServiceNow integration user:** least-privilege role, scoped to the configured tables and journal fields. `doctor` reports any write permission beyond `work_notes`, `comments` and the mapped fields.

---

## 8. Sync, takeover, feedback, and value baseline

### 8.1 Sync — poll-based, overlap-windowed, history-aware

Inbound ServiceNow is **poll-based, not webhook-based**: a DB-backed lease scheduler polls each configured table (`incident`, `em_alert` if Event Management is used, and any request tables) with **its own cursor**, batched and tenant-scoped.

- **Overlap, not strict `>`:** `sys_updated_on` has one-second granularity, and commits can land out of order. Each poll reads `sys_updated_on >= last_seen_cursor − overlap` (overlap 2 min, prov.) and **dedups on `(sys_id, sys_mod_count)`** in a `sync_seen` table. A record updated in the same second as the cursor, or committed late, is still picked up, and re-reading a record we already processed is a no-op.
- **History, not just current state.** A poll sees the record's *current* state, so the last writer can hide earlier writers. A human's state change followed by our work note in the same poll interval would look like an integration-user update. For every changed record, sync therefore reads **`sys_audit` (field changes) and `sys_journal_field` (comments and work notes) since the last processed `sys_mod_count`**. Takeover detection (§8.2) and `diff` feedback (§8.3) run on that **per-change history** with its actor, never on the record's last `sys_updated_by`.
- **Self-echo filter, per change:** changes whose actor is our integration user are recognized as **our own writes** and skipped, while the cursor still advances. This kills the feedback loop where the agent posts a work note, sync sees it, and the agent reacts to itself. Because the filter runs on the change history, a human change in the same interval is still seen.
- **What it costs:** poll interval (60 s, prov.) adds directly to MTTA and to the "alert → first proposal" SLO (§11), and each table and tenant consumes ServiceNow REST quota. `doctor` measures the instance's rate-limit headroom at onboarding and sets the interval. The SLO accounts for the poll interval explicitly.
- **Loss model, stated precisely:** there is **no webhook-loss window**, because there is no webhook. A record can be *delayed* (by up to the poll interval plus processing) but not *missed*, because of the overlap window and `(sys_id, sys_mod_count)` dedup. The cursor lives in the same database as the state it protects, so a crash loses at most the in-flight batch, and the next poll re-covers it. If `sys_audit` retention on the instance is shorter than our downtime, history for that gap is lost. `doctor` reports audit retention.
- **No customer-side business rules required:** the customer configures table names and field mappings. The interpretation (what counts as takeover, what is feedback) lives in *our* code, versioned and tested.
- **Case lifecycle** (driven by synced changes, not just our writes): `open → triaged → in_progress → human_owned → resolved → reopened`. Every state except `resolved` is covered by the dedup index (§4.1).
- **Backups:** **Litestream** continuously replicates the SQLite WAL to local or S3-compatible storage. It is an ops item from v0.1; the named, CI-tested PITR capability (restore to a point before an injected corruption) is v0.2.

### 8.2 Takeover and hand-back

- **Human actor, defined.** An actor counts as human only if it is **not** our integration user, **not** `system`, and **not** on the tenant's **automation-actor list**. `doctor --discover-automation` builds that list at onboarding from the partner's instance (assignment rules, predictive intelligence, workflow and flow users, other integrations), and a human admin confirms it. Many instances auto-assign tickets at creation. Without this list, every ticket would look "taken over" before the agent proposed anything.
- **Narrow takeover triggers (the only ones that count), evaluated on the per-change history (§8.1):** (1) a **human actor** changes `assigned_to` (to themselves or another human); (2) a **human actor** changes `state`. An *automation* changing `assigned_to` at creation is **not** takeover. A ticket *comment* by a human is **not** takeover either; the agent may still act within its grants.
- **`human_owned` is a hard state for sends and proposals.** On takeover the agent **stops proposing and stops sending** for that case. In-flight outbox rows that are already `approved` complete; we don't abort mid-send, we stop *new* work. Two things continue:
  - **Cadence drafts.** If an approved comms plan has an update due, the agent renders it as a **draft for the owner** in the case view (no outbox row, nothing sent), so comms debt is visible to the human instead of silently dropped.
  - **Dedup.** Duplicate alerts still attach to the case (the index covers `human_owned`, §4.1), so the human sees them on the case they are working.
- **Hand-back is explicit.** The agent never silently resumes. Resuming requires a human actor re-assigning the ticket *to the agent's integration identity*, or a human clicking "hand back" in the admin UI (recorded with their `users.id`). A new matching alert while `human_owned` **does not** hand back; it is a duplicate on the human's case.
- **Takeover is a first-class event** (`sync_takeover`) in the trace, with the triggering change, actor and `sys_mod_count` recorded.

### 8.3 Feedback capture — the three moments

Feedback is captured **at defined moments**, not scraped opportunistically:

- **`approval`**: the approver's reason on approve/reject (free text, required on reject). This is the highest-signal, cheapest data we have.
- **`diff`**: proposal versus reality. The human's *actual* action in ServiceNow (field change, state change, action taken), taken from the **per-change history** (§8.1) with its actor and paired with the proposal it contradicted or confirmed. Auto-captured from the **resolve-time form** where present.
- **`outcome`**: resolved/reopened plus final classification, at case close.

All three are `events` rows (append-only, tenant-scoped, trace-linked), the same store as the rest of the system, so replay shows feedback in context. This is the input to the caged, de-prioritized learning loop (§10).

### 8.4 Value baseline — counterfactual and assisted, separately

Two different measurement regimes, never mixed:

- **Counterfactual (shadow mode):** the agent runs the full pipeline **without executing** any side-effecting action and without posting. It records what it *would have done* and *when*. Measured: **alert → first-proposal time**, **proposal correctness** against the human's eventual action (`diff`), and **triage agreement**. In shadow, **MTTA/MTTR delta is zero by construction** (nobody was waiting on the agent), so it is *not* a shadow metric.
- **Assisted delta (assisted mode), with a control group:** the agent executes approved actions on a **treatment** set of services or shifts, while a **control** set of comparable services or shifts runs human-only **over the same period** (the split is agreed with the partner up front). The value number is the **treatment-vs-control difference** in MTTA, time-to-first-customer-update and MTTR, reported with confidence intervals. A before/after comparison against last quarter is reported only as secondary context, because it is confounded by seasonality, staffing, incident mix and process changes.
- **Promotion to assisted is the partner's decision.** It is a commercial milestone, not an engineering one, tracked as a project dependency like data access (ROADMAP §5). The engineering exit criterion is "assisted mode is *ready* and the promotion gate has passed on shadow data"; the value number exists only once the partner turns it on.
- **Both are first-class `stats` outputs** with the mode recorded on every metric row, so a report never mixes the two. Triage agreement is a *quality* metric; the treatment-vs-control delta is the *value* metric.

---

## 9. LLM client

One thin `LLMClient`, `structured(prompt, schema, timeout, budget) → typed | None`, with OpenAI / Anthropic / Ollama backends, bounded retries, per-call token accounting into `events`, and a **fixture mode** for deterministic **code** tests (routing, gate, outbox). **Fixtures test code, not prompts.** Prompt behavior is tested with the real LLM under cache plus N samples (§11). Provider-agnostic and air-gap friendly (Ollama). No chain-of-thought dependence: every decision that matters is a typed slot or a deterministic rule.

Internal rules:

- **Redaction is a mandatory internal step** (render → **redact** → send); see INV-06 and §6.1. It is not a hook callers must remember.
- **Model identity is pinned and recorded.** Config names a **dated snapshot ID** (the provider's versioned model identifier, or a local snapshot tag), never a floating alias. The *resolved* model and version is written into every `llm` event. A floating alias that the provider can silently swap is a config validation failure.
- **Model deprecation runbook (pinning implies scheduled migrations).** Providers retire dated snapshots on a published schedule, typically within about a year. `doctor` and a weekly job check the provider's deprecation notices for every pinned ID, and warn at T−90 days. Migration procedure: (1) add the successor ID as a *candidate*; (2) run the full quality suite on the frozen eval snapshot (§11) against both; (3) produce a per-case diff and per-severity gate report; (4) human sign-off via ADR; (5) switch the pin and re-baseline. A retired model with no approved successor puts the affected slots into **safe-default** (INV-05), never onto an unpinned alias.

---

## 10. Learning loop (T3) — caged, and de-prioritized

```
feedback (3 moments, §8.3) ─► distill (recurring APPROVED sequences → runbook draft)
                          └► evolve  (prompt/KB candidate patches)
                                ▼
                 full eval suite + HUMAN sign-off   [TRAIN-ON-TEST GUARDRAIL: §10.1]
                                ▼
                 versioned deploy (versions row) → drift monitor → one-command rollback
```

### 10.1 Train-on-test guardrail

Distilled/evolved artifacts are gated on an **eval holdout that is disjoint from the incidents the artifact was derived from — by *time and signature*.** Train on incidents before time T; the holdout contains incidents after T **and** no case whose fingerprint (§4.1) or error signature shares a signature with a training case. Deriving a runbook from incident X and gating it on cases built from incident X (or from its near-duplicate twin) is train-on-test and is disallowed. This is the single biggest integrity rule in the learning loop.

**Priority:** the learning loop is **not** the signature feature. It is the *riskiest, lowest-proven-value, most Goodhart-prone* capability. **Case memory (C12) + knowledge capture (C15)** are the high-value core and ship first; **runbook distillation (C13)** is v0.2 *behind* the guardrail; **prompt/KB self-evolution (C14)** is a **research track** — in scope, eval-gated, human-signed, but **explicitly not the v1.0 exit story** (v1.0 is the value story: invariants + assisted-delta metrics + audit trail). We keep C14 because it is a stated requirement; we no longer oversell it.

**Non-learnable surface (hard rule):** the approval gate, tier model, tool trust classes, approval policy, RBAC, eval-suite structure, **routing table, and taxonomy** can **never** be modified by the loop — only by a human-reviewed PR.

---

## 11. Evals — invariants vs quality, and an honest data story

> **ADR-0011:** reliability is measured in six layers (invariants, contracts, pack evals in the incident lab, scenario simulation, calibration, production feedback). Lab scenarios carry ground-truth root causes and are scored structurally; the false-cause rate is the strictest gate. A pack is enabled for assisted mode only after it passes its gates.

- **Two suites, clearly separated:**
  - **Invariants suite (INV-01..06):** hard gates, single sample, must pass, red blocks merge, run against **both engines**. INV-01–05 are code behavior. **INV-06 (LLM-bound canaries) is the one that touches the LLM path**, and it is still a *measured* wiring property (canary tokens must be absent from captured provider requests), not prompt quality. This is the "invariant regressions never ship" story, and it is *true*, because invariants are code behavior, not prompt behavior.
  - **Quality suite:** classification, severity and comms quality, run **N times per case** (stochastic). Reported as a **per-case majority** and then a **Wilson lower bound computed over cases, not cases × N** (the N samples of one case are not independent observations). It also uses a **severity-cost matrix** (a P1→P3 misgrade weighs far more than P3→P1), and tracks **citation-faithfulness rate** (§5.4), **pre-classifier conflict and abstention rates** (§4.2), **entailment-checker accuracy** on its curated pair set (§5.4), **redaction detector recall** on the held-out secret corpus (§6.1), latency (per stage, see the SLO below) and cost. It feeds the **per-label `calibrated_score` table** (§4.2).
- **Frozen eval snapshot.** The rendered prompt includes retrieved case memory and KB content, so evals run against a **versioned, frozen snapshot** of the KB, case memory and service catalog (`evals/snapshots/<id>/`), pinned in the suite manifest. Without it, every newly resolved case would change rendered prompts, invalidate the cache, and make results irreproducible. The snapshot is bumped deliberately (an ADR), never implicitly.
- **Prompt changes are tested with the real LLM.** The cache is keyed on **rendered-prompt hash + model ID + resolved model version + snapshot ID**, and any change invalidates the cache and forces a live run. **Fixtures never validate prompt quality.**
- **Release gates: per severity, provisional, and sized to the math.** All thresholds are **(prov.)** and are re-set from v0.0 shadow data via an ADR. The suite is sized so the gates *can* be passed. With a Wilson 95% lower bound, a perfect score on n cases gives LB = n / (n + 3.84):

  | Gate (prov.) | Threshold | Cases for a perfect score to pass | Cases to tolerate one miss | Planned holdout |
  |---|---|---|---|---|
  | P1/P2 classification correctness | LB ≥ 0.90 | ≥ 35 | ≈ 52 | **≥ 60 P1/P2 cases** |
  | P1/P2 severity correctness | LB ≥ 0.85 | ≥ 22 | ≈ 33 | (same 60) |
  | Citation faithfulness | LB ≥ 0.95 | ≥ 73 pairs | ≈ 100 | **≥ 120 claim/evidence pairs** |
  | Comms quality (DLP + faithfulness + tone) | pass@3 per case | — | — | ≥ 40 comms cases |
  | Per-label coverage | ≥ 10 holdout cases per label | — | — | grows with the taxonomy |

  **Planned v0.1 holdout: 150–250 cases**, drawn primarily from the design partner's corpus, with a P1/P2-enriched sample (P1/P2 are a minority of real volume, so they are deliberately oversampled). A label with too few holdout cases has no `calibrated_score` entry and therefore safe-defaults (§4.2), so coverage gaps are fail-closed. A new label ships with its holdout cases, not after.
- **LLM spend breaker (part of C20):** a per-tenant daily token/cost budget in config. When it is breached, the system degrades to **safe-default-only**: safe-default classification, `enrich_page` attach-only, no new proposals, and a `spend_breaker` event. It is a component, not a feature flag. It cannot be configured off; only its threshold can move.
- **SLO (prov.):** triage p95 (alert visible in ServiceNow → first actionable proposal) with **per-stage budgets**: poll interval ≤ 60 s (§8.1), dedup < 50 ms, pre-classifier < 10 ms, classify p95 < 15 s, RCA-lite p95 < 60 s. A breach is logged as an SLO-miss event, never silently. Budgets are re-set from v0.0 measurements on the partner's instance.
- **Honest data story:** GitHub and Atlassian **status pages are post-hoc customer-facing summaries**, so they give real *labels*, not raw payloads, logs or CI output. "≥50% real incidents" from those sources would be mostly **real labels on synthesized inputs**. The **real corpus is the design partner's 6-month incident history** (tickets, alerts, logs, resolutions). That data is a **prerequisite dependency** (CAPABILITIES "Hard dependency"; ROADMAP risk section), not a nice-to-have.
- **Licensing:** the Google SRE materials are **CC BY-NC-ND (non-commercial)**, which is a real risk for a commercial product's eval suite and sales demo. We **cite the 68% change-induced statistic as a factual reference**, never redistribute or adapt the text, and do not build eval cases from SRE Book text. The corpus is design-partner data plus status-page data.
- **We do not weight the eval mix by the prior under test** (that would be train-on-test of the prior). The prior is a per-deployment parameter; the eval mix is broad and realistic.

---

## 12. Taxonomy — the contracts

The `classification` label is the highest-leverage contract in the system (routing, comms and retrieval all key off it). It is **defined, versioned, and eval-anchored**:

```
<domain>/<signal>     e.g. database/saturation, deploy/failure, network/latency, request/access
severity:  P1 | P2 | P3 | P4        (cost-matrix ordered, §11)
```
- The taxonomy is **data** (a versioned file in the repo), not prose; adding a label is a reviewed PR.
- Every routing row, runbook `applies_when`, and comms template **references a taxonomy entry**, validated in CI (no dangling references).
- The **pre-classifier** (§4.2) and the **eval quality suite** both key on the same taxonomy, so "did we misgrade" is well-defined.

### 12.1 Hypothesis taxonomy (the second contract — for RCA steps)

The RCA plan (§5) may only reference **hypothesis IDs from a fixed, versioned set**, kept in a second contract file alongside classification:

```yaml
# taxonomy/hypotheses.yaml (data, versioned; adding/renaming an ID is a reviewed PR)
- id: recent_change_push          # binary/config/flag push within the window
  excludes: []
- id: capacity_decay              # slow resource exhaustion, no recent change
  excludes: [recent_change_push]  # by definition: "no recent change"
- id: dependency_failure          # internal dependency degraded/down
  excludes: []
- id: upstream_provider           # vendor/provider incident
  excludes: []
- id: traffic_spike               # load anomaly
  excludes: []
- id: data_volume                 # dataset/row-count growth
  excludes: []
```

- `excludes` is the **declarative conflict set** used by the `reduce()` truth table (§5): if two hypotheses that exclude each other are both SUPPORTED, the outcome is **INCONCLUSIVE**, with both recorded.
- The LLM may propose hypotheses **only from this ID set** (§5); free-text hypotheses are not allowed.
- `PROBES[hypothesis]` (registered probe: tool + args + CEL `check`) lives in runbook config and is **CI-validated**: a probe naming an unregistered tool or a non-taxonomy ID fails validation.
- This set is **non-learnable** (§10): the learning loop cannot add or rename hypothesis IDs; only a human-reviewed PR can.

### 12.2 Action kinds (the third contract — one canonical enum)

Every action kind used anywhere (proposals, outbox, grants, docs, tests) comes from **one enum**, `core/kinds.py`, with its tier and audience. CI greps the three docs and the code for action-kind names and fails on any name not in the enum (`test_docs_action_kinds_canonical`).

| Kind | Tier | Audience | Authorization path | Notes |
|---|---|---|---|---|
| `work_note` | T1 | internal | grant (customer-approved at setup) or approval | ServiceNow `work_notes`, not caller-visible |
| `enrich_page` | T1 | internal | grant or approval | attaches to an *existing* page only (§14) |
| `watch_list_add_internal` | T1 | internal | grant or approval | adds an internal user (resolved via `users`/catalog owners) |
| `watch_list_add_customer` | T2 | **customer** | approval only | adding a customer user sends them email |
| `customer_comment` | T2 | **customer** | approval or approved comms plan | ServiceNow `comments`, caller-visible |
| `send_email_customer` | T2 | **customer** | approval or approved comms plan | |
| `create_ticket_customer` | T2 | **customer** | approval only (two-person, v0.2) | |
| `ticket_update` | T2 | internal | approval or literal-scope grant | field updates other than watch list |
| `job_rerun` | T2 | internal | approval or literal-scope grant (two-person, v0.2) | Airflow / GHA `rerun_failed_only` |
| `fulfill_request` | T2 | depends on the catalog item | approval or literal-scope grant | v0.2, C7 |
| `comms_plan` | T2 | (meta) | approval only | the plan itself (§6.3) |
| `create_page` | — | — | **never executable in v0.x** | non-goal (§13, §14) |

- `CUSTOMER_FACING_KINDS = {watch_list_add_customer, customer_comment, send_email_customer, create_ticket_customer}`. These are never grant-authorized (trigger, §6.2).
- `NEVER_EXECUTABLE_KINDS = {create_page}` (trigger, §6.2).
- Both sets are generated into the trigger literals at migration time (`test_trigger_kind_lists_match_code`).

---

## 13. Non-goals (v0.x)

We will **not** do these in v0.x, on purpose:

- **Not a pager.** SupportAgent never creates pages in v0.x. It enriches pages that the customer's own monitoring created (§14).
- **Not an auto-remediator.** State-changing actions are always gated or covered by a literal-scope, customer-approved grant (§2, INV-01).
- **Not a general agent or chatbot.** Bounded loops, typed slots only.
- **Not multi-tenant-operated in v0.1.** The *schema* is tenant-ready from day one, but v0.1 runs single-tenant (§15).
- **Not a replacement for ServiceNow/ITSM.** We enrich and act *through* it, bidirectionally (§8).
- **No unattended self-modification of the control plane** (gate, tiers, policy and RBAC are non-learnable, §10).
- **No customer-facing statements of root cause from RCA outcomes** (§5). Humans write those.

---

## 14. The paging rule — enrich only

**SupportAgent never creates pages in v0.x.** Paging stays with the customer's own monitoring (PagerDuty, Opsgenie, or ServiceNow on-call firing on their alerts), and **an external paging integration is a deployment prerequisite**: `doctor` checks that one is configured for every service in the catalog, and refuses to start triage for a service without one.

- **`enrich_page`: T1, policy-allowed after setup approval (fire-and-track).** It *attaches context to an existing page only* (case link, triage summary, top hypotheses, suggested actions), matched to the page by dedup key or by the alert's source ID. It cannot create a page, pick recipients, or change routing. Even a misfire only adds a note to a page that the customer's monitoring already created.
- **"Should someone be paged?" is a recommendation, not an action.** When the agent's triage suggests a page that does not exist (for example a free-text P1 ticket with no matching alert), it raises a **`page_recommended`** banner on the case in the admin UI and a `work_note`. A human decides whether to page through their own tool. The agent cannot page, so a 1,000-alert storm cannot page 1,000 times, and an injected ticket body cannot wake anyone.
- **`create_page` is reserved, not implemented.** It exists in the enum only so that the trigger can forbid it (`NEVER_EXECUTABLE_KINDS`, §12.2; `test_create_page_never_executable`). Revisiting this is a post-v1.0 ADR with its own threat model, covering rule-only authorization, rate limits and pager-DoS.
- **On-call schedule lookup** is a first-party read tool (via the service catalog `oncall_schedule`, §7), used to show *who is on call* in the case view and in `enrich_page` content.
- **Maintenance windows and change freezes:** during a freeze, the `recent_change_push` prior is zeroed and the zeroing is logged (§5.5).

This keeps the product safe at 3 a.m. because the 3 a.m. path never depends on it. It is a **non-learnable surface** (§10): the loop cannot add page creation or widen `enrich_page`.

---

## 15. Data model (canonical; tenant_id on every table from day one)

This is the single source of truth for the schema. The DDL in §4.1, §4.3, §6.2 and §6.3 is the authoritative column-level definition for the dedup, grouping, gate and comms tables; this list is the **complete** table set.

```
-- Identity & RBAC (§6.4)
users(id = oidc_sub, tenant_id, name, email, is_active)
role_assignments(user_id→users, tenant_id, role, scope)
policy_grants(id, tenant_id, action_kind, scope /*literal*/, status, requested_by,
              requested_at_ts, granted_by→users?, granted_at_ts?, valid_to_ts?)   -- §6.2
-- (no customer_facing_kinds table: the kind lists are trigger literals generated from code, §6.2/§12.2)

-- Case & evidence
cases(id, tenant_id, fingerprint, classification, severity, status, group_id→case_groups?,
      service→services?, duplicate_count, opened_ts, last_alert_ts, resolved_ts)
      + UNIQUE INDEX ux_cases_unresolved (tenant_id, fingerprint) WHERE status <> 'resolved'  -- INV-02, §4.1
case_groups(id, tenant_id, opened_ts, closed_ts, rule, member_count)          -- §4.3
alerts(id, tenant_id, case_id, adapter, payload_ref, received_ts)
sync_cursors(tenant_id, table_name, cursor_ts, lease_owner, lease_until_ts)   -- §8.1
sync_seen(tenant_id, sys_id, sys_mod_count, processed_ts)  PK(tenant_id, sys_id, sys_mod_count)

-- Gate / approval / outbox (§6.2)
proposals(id, tenant_id, case_id, action_kind, scope, payload_json,
          payload_hash, trace_id, status, created_by, created_at_ts)
approvals(id, tenant_id, proposal_id→proposals, approver_id→users,
          status, payload_hash, reason, approved_at_ts)
          + TRIGGER trg_no_self_approval
outbox(id, tenant_id, case_id, action_kind, scope, payload_json, payload_hash,
       idempotency_key UNIQUE, status,
       approval_id→approvals UNIQUE, policy_grant_id→policy_grants, comms_plan_id→comms_plans,
       CHECK (exactly one authorization column set))                          -- INV-01
       + TRIGGER trg_outbox_inv01 (INSERT semantic check)
       + TRIGGER trg_outbox_mark_executed (single-use)
       + TRIGGER trg_outbox_immutable (UPDATE)

-- Comms (§6.3): every send is an outbox row
comms_plans(id, tenant_id, case_id→cases, audience, channel, template_id,
            allowed_fields_json /*each field with source*/, cadence_minutes,
            plan_hash, proposal_id→proposals, approval_id→approvals?, status)
comms(id, tenant_id, case_id→cases, kind, plan_id→comms_plans?,
      outbox_id→outbox UNIQUE, body_json, status, sent_at_ts)

-- Observability (append-only, INV-03)
events(id, tenant_id, trace_id, span_id, parent_span_id, kind, actor,
       model, tokens_in, tokens_out, latency_ms, status, payload, ts)
    kind: ingest | dedup | preclass | classify | decision | llm | redact |
          tool_call | rca_step | gate | send | sync | sync_takeover | feedback |
          prior_zeroed | page_recommended | spend_breaker | webhook_rejected |
          executor_hash_mismatch | slo_miss | onboarding
    (`onboarding`, ADR-0017: onboarding's audit trail, one trace per tenant; `payload.action`
     is a step, a plugin installed or disabled, an access or graph decision, readiness,
     calibration or drift)
    (`redact` events are emitted from inside LLMClient: counts per detector class, never the values)

-- Knowledge & learning
nodes(id, tenant_id, kind, ref, label, updated_ts)
edges(src, dst, tenant_id, kind, weight, created_ts)
feedback(id, tenant_id, case_id, kind[approval|diff|outcome], payload, ts)   -- §8.3
versions(id, tenant_id, artifact, sha, eval_report, holdout_disjoint_check,
         approved_by→users, active, ts)                                       -- §10.1
kb_fts   -- FTS5 over normalized runbooks/playbooks/cases

-- Catalog (§7)
services(name, tenant_id, repos, log_paths, dags, cmdb_ci,
         oncall_schedule, owners, depends_on)
automation_actors(tenant_id, sys_user, kind, confirmed_by→users)              -- §8.2
```

**INV-02 enforcement:** the partial unique index `ux_cases_unresolved` (DDL in §4.1) together with the §4.1 upsert is what makes dedup a *database property* rather than an advisory check. `test_dedup_single_case`, `test_dedup_concurrent_no_second_case`, `test_dedup_human_owned_no_second_case` and `test_human_tickets_never_collapse` (CI, both engines) prove it.

**Migrations:** Alembic from day one (no hand-edited schema). Triggers ship as migrations in **two dialects** (SQLite and PL/pgSQL), and the invariant suite runs against both (§2). **Backups:** the SQLite file is the system of record. **Litestream** continuous WAL replication is an ops item from v0.1; the named, CI-tested PITR capability is v0.2 (§8.1). **Scheduling:** comms cadence, expiry sweeps and sync polls use a **DB-backed lease** (not a bare in-memory APScheduler timer), so multiple replicas don't thunder-herd.

**Retention & crypto-shredding:**
- **Retention** is per-tenant config (`retention.events_days`, `retention.comms_days`, `retention.alerts_days`). A DB-lease expiry sweep deletes rows past their window. Audit rows (`approvals`, `policy_grants`, `gate` events, `versions`) keep a longer floor.
- **Crypto-shredding:** raw payloads (`alerts.payload_ref` targets, `events.payload`) are stored **encrypted at rest with a per-tenant key**. Deleting a tenant means **destroying the key**, so every stored payload becomes unrecoverable *even if the DB is retained* for audit. This answers "we can't delete from a backup": the backup becomes unreadable.

---

## 16. Tech stack

| Layer | Choice |
|---|---|
| Language/tooling | Python 3.12 · uv · ruff · pytest · Typer (CLI) |
| API | FastAPI + HTMX admin (approvals, stats, graph view — no SPA) |
| Types | Pydantic v2 — `AlertEvent` / `TriageResult` / `Action` are the contract |
| Storage | SQLite default, Postgres via flag (invariant suite must pass on both, §2) · SQLAlchemy · **Alembic** migrations (triggers in both dialects) |
| Search | SQLite FTS5 (BM25) + normalized-signature index + optional sqlite-vec |
| LLM | thin client: OpenAI / Anthropic / Ollama · structured output via JSON schema |
| Tools | MCP (streamable-HTTP client, ADR-0012) + first-party in-process tools |
| Runbook checks | **CEL** (`cel-python`) — runbook `check:` expressions compiled in CI against tool result schemas (§5); *not* Python eval |
| Durability | **Litestream** (continuous WAL → local/S3) for the SQLite system of record: ops item v0.1, CI-tested PITR v0.2 (§8.1, §15) |
| Scheduling | **DB-backed lease** (comms cadence, expiry sweeps, sync polls) |
| Tracing | own append-only store + optional OTLP export |
| Secrets | **Vault / env-injected; MCP `command:` servers privilege-restricted** |
| Packaging | one Docker image + `supportagent.yaml` + env secrets · `supportagent doctor/eval/replay/stats` |

**Deliberately not used in v0.1:** LangGraph/CrewAI (the state machine *is* the product), Kafka (outbox suffices), **Helm/K8s (deferred — single container first)**, managed vector DB (FTS5 + signature index first), LangSmith as a dependency (export to it). **Helm, multi-tenant operation, MCP (until a customer brings a server), and the full comms matrix are deferred** out of v0.1 (see ROADMAP re-cut).

---

## 17. Capability → component map

| Cap | Component(s) |
|---|---|
| C1 triage | `redact` + `dedup` + `preclass` + `pipeline` (Loop A) + `routing` + `llm` |
| C2 RCA | `rca` (Loop B) + `kb` (runbook YAML) + `tools` |
| C3 correlation | `kb/graph` (`shares_change`/`similar`) + `rca` deep — v0.2 |
| C4 comms engine | `comms` (plans as approved proposals; every send an `outbox` row) + field-allowlist DLP (pattern DLP v0.2) |
| C5 duty loop | `comms/scheduler` (DB lease) + `enrich_page` / `page_recommended` (§14) |
| C6 job re-run | `rca` (evidenced proposal) + `gate` + `tools` (airflow/gha) — **`cron` deferred** |
| C7 service requests | `routing` (request rows) + `kb/catalog` + `outbox` |
| C8 ticket ops | `outbox` + `tools/servicenow` + `sync` (§8) |
| C9 MCP integration | `tools/mcp_client` (deferred until a customer needs it) |
| C10 skill docs | `kb` (runbook YAML + prose) + retrieval |
| C11 feedback | `sync` + `memory/feedback` (real capture, §8.3) |
| C12 case memory | `memory/cases` + normalized-signature retrieval |
| C13 runbook distillation | `learning/distill` (**+ train-on-test guardrail**, §10.1) |
| C14 prompt/KB evolution | `learning/evolve` (**research track, caged, de-prioritized**, §10) |
| C15 knowledge capture | `learning/capture` (case → KB draft, gated) |
| C16 shadow mode | `shadow` runner (parallel Loop A, no outbox) + **counterfactual value baseline** (§8.4) |
| C17 simulate/dry-run | `tools/simulator` (read-only previews on the approval screen) |
| C18 redaction (INV-06) | `redact` inside `LLMClient.structured()` (§6.1) + **canary suite** (§19) |
| C19 sync + human takeover | `sync` (§8: overlap-windowed polls, `sys_audit` history, automation-actor list, `human_owned` hard-stop + owner drafts) |
| C20 alert-storm handling | `dedup` + `case_groups` (deterministic grouping, no LLM, §4.3) + LLM spend breaker (§11) |
| C21 approver RBAC + no-self-approval | `gate` + `rbac` + `trg_no_self_approval` (§6.2, §6.4); two-person rule v0.2 |
| C22 value baseline | `shadow` + `stats`: **counterfactual (shadow)** and **assisted treatment-vs-control delta (live)** as separate first-class outputs, mode recorded (§8.4) |

---

## 18. Repository layout

```
supportagent/
  pyproject.toml            # uv; single image
  supportagent.yaml         # customer config: routing, tiers, comms, tools, taxonomy ref
  core/
    kinds.py                # canonical action-kind enum + CUSTOMER_FACING / NEVER_EXECUTABLE sets (§12.2)
    redact.py  dedup.py  groups.py  preclass.py
    pipeline.py             # Loop A
    rca.py                  # Loop B
    routing.py              # deterministic label→pathway (pure, unit-tested)
    gate.py  rbac.py  outbox.py  comms/{planner.py, scheduler.py, dlp.py}
    sync.py                 # bidirectional ticket events → case lifecycle
    llm.py                  # LLMClient + fixture mode
    trace.py  shadow.py
    memory/{cases.py, feedback.py}
    learning/{distill.py, evolve.py, capture.py}
    tools/{mcp_client.py, simulator.py,
           first_party/{logs,airflow,gha,servicenow,change_feed,oncall}.py,   # v0.1
           first_party/{flags,metrics}.py}                                     # v0.2
  kb/{runbooks.py, schema.py, retrieve.py, graph.py, normalize.py}
  evals/{harness.py, invariants/, quality/, cases/*.yaml, snapshots/<id>/, secrets_heldout/}
  api/{app.py, routes/, ui/}           # FastAPI + HTMX
  cli/{doctor.py, eval.py, replay.py, stats.py}
  migrations/                    # Alembic; triggers in SQLite + PL/pgSQL
  docs/
    spec/                        # ARCHITECTURE.md (this file), CAPABILITIES.md, ROADMAP.md
    adr/                         # post-freeze changes: one ADR per code PR that changes the spec
    reviews/                     # design reviews critique-v1..v3
    visual/blueprint.html        # visual guide
    CHANGELOG.md  PITCH.md
  taxonomy/                      # versioned contracts (§12) — reviewed PRs only
    classification.yaml          #   domain/signal + severity (P1–P4)
    hypotheses.yaml              #   RCA hypothesis IDs + excludes (§12.1)
  catalog/
    services.yaml                #   service catalog: repos, log_paths, dags, cmdb_ci, oncall, owners, depends_on (§7)
  runbooks/  playbooks/          # the KB (YAML + prose)
  tests/  docker/
```

---

## 19. CI gates (measured, not promised)

The invariants are **enforced in CI by tests, not by prose**, and every invariant job runs against **both SQLite and Postgres**:

| Gate | What it proves | Tests |
|---|---|---|
| INV-01 gate | no outbox row without exactly one valid authorization; approvals single-use; authorization and payload immutable; bytes sent = bytes approved; customer-facing kinds never grant-authorized; `create_page` never executable; no self-approval | `test_gate_rejected_approval_blocked`, `test_gate_expired_approval_blocked`, `test_gate_payload_hash_mismatch_blocked`, `test_gate_grant_expired_or_out_of_scope_blocked`, `test_gate_grant_pending_not_enforceable`, `test_gate_approval_single_use`, `test_outbox_immutable_after_insert`, `test_executor_rehash_mismatch_aborts`, `test_customer_comm_never_by_policy_grant`, `test_comms_plan_revoked_blocked`, `test_plan_autosend_fields_from_record_state`, `test_self_approval_blocked`, `test_create_page_never_executable`, `test_trigger_kind_lists_match_code`, `test_fk_pragma_on_every_connection` |
| INV-02 dedup | one unresolved case per fingerprint, even concurrent, even while `human_owned`; human tickets never merged | `test_dedup_single_case`, `test_dedup_concurrent_no_second_case`, `test_dedup_human_owned_no_second_case`, `test_human_tickets_never_collapse` |
| INV-03 trace | every side effect, LLM call, tool call and decision has an `events` row | `test_every_action_traced` |
| INV-06 redaction | **the redaction path is wired into every LLM call site**: no detector-positive canary reaches a provider | `test_llm_bound_canaries_absent`. Detector *recall* on the held-out realistic corpus is a quality-suite metric, not this gate (§6.1) |
| INV-04/INV-05 failure | `SUPPORTAGENT_FAULT_INJECT=llm_timeout,sn_500,gha_500` → **100% safe default, 0 crashes** | fault-injection job |
| storm soak | 1,000-alert burst across many fingerprints → **0 duplicate cases, LLM calls ≤ groups + headroom, 0 double-sends (effectively-once)** | soak job |
| sync | overlap-window poll picks up same-second and late-committed updates; a human change hidden behind an integration-user write is still detected; automation assignment is not takeover | `test_sync_same_second_not_missed`, `test_sync_human_change_behind_echo_detected`, `test_sync_automation_assign_not_takeover` |
| docs | every action-kind name in the docs exists in `core/kinds.py`; every CHANGELOG "where it landed" anchor resolves | `test_docs_action_kinds_canonical`, `test_changelog_anchors_resolve` |

**All of the above are CI gates, not manual demos.** A red gate blocks merge. Week 1 of v0.0 starts by committing these tests as **failing tests**; from then on the invariant suite *is* the spec, and this table is its index.
