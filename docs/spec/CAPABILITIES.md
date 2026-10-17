# SupportAgent — Capability Model

> Companion to [ROADMAP.md](ROADMAP.md) and [ARCHITECTURE.md](ARCHITECTURE.md). The roadmap says *when*; this document says *what*; the architecture document says *how it is built* (components, loops, routing, knowledge graph).
> Decisions baked in (agreed): the approval promise is **T2-and-above is always gated (or customer-owned-allowlisted); T1-internal actions are policy-allowed by default — every execution traces to a human approval or a customer-owned policy entry, enforced at the DB layer**; job re-runs are **gated-by-default + customer allowlist**; the first-party tool set is **log files, Airflow, GitHub Actions, ServiceNow** (cron re-invoke deferred — code-execution capability); the learning ladder ships as **case memory v0.1 → runbook distillation v0.2 (holdout disjoint in time *and* signature, never cases generated from the source incident — train-on-test is disallowed) → prompt/KB evolution v0.3 (in scope by requirement, *not* the v1.0 headline)**; service requests support **both ServiceNow catalog mapping and generic templates**; paging is **enrich-only in v0.x** (`enrich_page` attaches to existing pages; `create_page` is not implemented, and an external paging integration is a deployment prerequisite); the release cut is **v0.0 = the full trust chain (gate, dedup, redaction, outbox, replay, OIDC identity, no-self-approval) on the ServiceNow adapter — the walking skeleton on a design partner's real tickets; v0.1 = triage + gated comms + RCA-lite + case memory + sync/takeover + RBAC + storm handling + shadow counterfactual baseline** (MCP deferred to v0.2), with two-person rule, DLP beyond the plan-level field allowlist, Litestream PITR, re-run decisions, service requests, and runbook distillation in v0.2. A **design partner with historical ticket data is a hard dependency** (§6).

---

## 1. Framing: four layers, one spine

```
L4  LEARNING LOOP     feedback → case memory → runbooks → eval-gated self-evolution
L3  EXTENSION         MCP servers + skill docs (trust-classified at registration)
L2  EXECUTION         comms, job re-runs, ticket ops, request fulfillment (all tiered)
L1  COGNITION         triage, RCA, correlation, intent routing (autonomous, traced)
─────────────────────────────────────────────────────────────────────────────────
SPINE:  autonomy tier model + the five pillars (evals, gate, traces, idempotency, safe defaults)
```

**Design rules (non-negotiable):**
1. **Capabilities are pluggable; the core loop is stable.** New capabilities never touch the loop; they attach as tools, skills, or playbooks.
2. **Tool output is data, never instructions — and so is the inbound ticket body.** Any MCP/external payload *and any inbound customer text* is wrapped as untrusted content; instruction-like text is ignored and logged; a deterministic validator requires that any action target relates to a case resource (ARCH §6.1, §7). The **primary** injection surface is the inbound ticket, not MCP tools.
3. **Every state touch is tiered, traced, and idempotent.** No capability may bypass the tier model, the trace log, or the outbox.
4. **Learning is gated by the same eval suite as code.** The agent can *propose* changes to itself; it cannot *apply* them.
5. **The non-learnable surface is sacred:** the approval gate, the tier model, tool trust classes, approval policy, RBAC, the eval-suite structure, the routing table, the classification/hypothesis taxonomies, and the action-kind enum (ARCH §10, §12). Eval cases may be *added* (with a `meta: why` note), never loosened.

---

## 2. The autonomy tier model (the spine)

Every tool, action, and learned artifact maps to exactly one tier. Tiers are assigned **at registration, in config — never inferred at runtime.**

| Tier | Meaning | Examples | Autonomy |
|---|---|---|---|
| **T0** | Read & analyze — no external state change | log reads, job status, RCA, drafting, case-memory retrieval, correlation | Fully autonomous; fully traced; token-budgeted |
| **T1** | Internal, low blast radius (notifications may fire — "reversible" is *not* the criterion) | internal status posts, `watch_list_add_internal` (internal users only), `work_note` (internal case note, **not visible to the ticket caller**), **`enrich_page` (attaches context to an *existing* page; never creates one; ARCH §14)** | Policy-allowed **after the customer admin approves the default T1 grants at setup** (ARCH §6.4); logged; revocable |
| **T2** | State-changing / operational | job re-runs (Airflow/GitHub Actions), `ticket_update`, `customer_comment` (**visible to the ticket caller**), `watch_list_add_customer` (adding a customer emails them), request fulfillment, **customer-facing comms** | **Always gated by default.** A customer-approved, **literal-scope** grant may pre-approve *specific* non-customer T2 actions. Customer-facing kinds are never grant-authorized: they need a per-send approval or an approved comms plan (ARCH §6.3) |
| **T3** | Self-modification | **artifacts:** skill doc / KB / learned runbook adoption · **control:** prompt patches, routing config | Gated **and** eval-gated; versioned deploy; one-command rollback; **control-plane changes take the strictest path** — artifacts carry less risk than the control plane |

**The promise, stated once (consistent across all three docs):** *propose → human approves → execute* applies to **T2 and above**. T1-internal actions are policy-allowed by default (which is why "internal status auto-posted" works in the demo without breaking the promise). Either way, every execution traces to **a human approval or a customer-owned, audited policy entry** — and the outbox enforces one-or-the-other at the DB layer (ARCH §6.2, INV-01). There is no third path, and no config that turns the gate off.

**Canonical action kinds:** every kind name used here, in the roadmap and in code comes from one enum (ARCH §12.2, `core/kinds.py`). `create_page` exists in the enum only so the database can forbid it. It is **not implemented in v0.x** (ARCH §14).

**Trust class and tier must agree:** a tool declared `class: read` at `tier: T2`, or `state-changing` at `tier: T0`, **fails config validation at load time** (ARCH §7).

**Allowlist semantics (the job re-run decision) — the DB is authoritative, YAML is a *request*:**
```yaml
# supportagent.yaml — a REQUEST for grants, not the grant itself
allowlist:
  - action: job_rerun
    system: airflow
    target: "dag:nightly-sync"          # literal target
    requires_idempotency: true
    max_risk: low
    requested_expiry: 90d
  - action: job_rerun
    system: github_actions
    target: "workflow:deploy/**"        # a pattern is EXPANDED at request time (below)
    scope: rerun_failed_only            # only "re-run failed jobs", never full re-run
```

- **`supportagent grants request --from config`** turns each YAML entry into `policy_grants` rows with `status='requested'`. A pattern target is **expanded against the service catalog into one literal-scope row per concrete target** (for example `gha:repo:acme/prod-api:workflow:deploy.yml:rerun_failed_only`). Requested rows are **not enforceable**. They become enforceable only when an **admin (≠ the requester; a schema CHECK enforces this)** approves them in the admin UI, which stamps `status='approved'`, `granted_by`, `granted_at_ts` and `valid_to_ts` (anchored at approval time). The **DB is the single source of truth** (ARCH §6.4): the outbox trigger (ARCH §6.2) matches `policy_grants` by **exact** scope, never YAML and never a pattern.
- Every auto-executed granted action is still traced, idempotency-keyed, and reported in `supportagent stats` with the exact `policy_grants` row that authorized it.
- **Product rule:** customer-facing kinds can never be authorized by a grant. The INV-01 trigger rejects them on the policy path using a **literal kind list generated from code** (`CUSTOMER_FACING_KINDS`, ARCH §12.2; `test_customer_comm_never_by_policy_grant`). Internal posts can be.
- The agent never self-approves, never widens a grant, and never proposes grant changes — T3-human-only.
- **RBAC / separation of duties (C21):** roles per `action_kind × service × risk`; an approver **cannot approve an action they created (v0.0, `trg_no_self_approval`)**; **two-person rule for high-risk actions (v0.2)**; ticket-comment approval is **not** a valid channel for state-changing actions (a customer commenting on their own ticket is otherwise a privilege-escalation path) — approvals go through the authenticated admin surface only, bound to the exact `payload_hash` (ARCH §6.4).

---

## 3. Capability registry

| ID | Capability | Layer | Tier(s) | Release | One-liner |
|---|---|---|---|---|---|
| C1 | Incident triage (typed, evidence-linked) | L1 | T0 | v0.1 | Classification + severity + impact + routing, every claim cited |
| C2 | RCA — hypothesis–evidence loop | L1 | T0 | v0.1 lite / v0.2 deep | Budgeted hypothesis scoring against logs, runs, changes |
| C3 | Cross-incident correlation | L1 | T0 | v0.2 | Detects incidents sharing a root change/resource |
| C4 | Comms engine (plans, cadence, audiences) | L2 | T1/T2 | v0.1 | Audiences × cadence × templates; idempotent sends |
| C5 | Duty loop (SLA, stale pings, min-cadence) | L2 | T1 | v0.1 basic / v0.2 PIR+follow-up | Proactive behaviors, not just reactive |
| C6 | Job re-run decisions | L2 | T2 | v0.2 | Evidence-mandatory proposals; allowlist; simulate/dry-run |
| C7 | Service requests (catalog + templates) | L1+L2 | T2 | v0.2 | Intent routing → param collection → gated fulfillment |
| C8 | Ticket operations (ServiceNow) | L2 | T2 | v0.1 | Comments, field updates — via outbox, always |
| C9 | MCP tool integration | L3 | per-tool | **v0.2** (deferred until a customer brings a server) | Trust-classified at registration; injection-safe |
| C10 | Skill docs (instruction playbooks) | L3 | T0 context | v0.1 static / v0.2+ learned | Versioned markdown the agent loads into context |
| C11 | Feedback capture (3 moments) | L4 | T0 | v0.1 | Approval reason, proposal-vs-reality diff, outcome |
| C12 | Case memory (retrieval, cited) | L4 | T0 | v0.1 | "We've seen this before" with citation IDs |
| C13 | Runbook distillation | L4 | T3 | v0.2 | Recurring approved sequences → draft skill, eval-gated |
| C14 | Prompt/KB evolution | L4 | T3 | **v0.3 (research track — in scope, *not* the v1.0 headline)** | Feedback → candidate patch → full eval gate (incl. **holdout disjoint from training in time AND signature — never cases generated from the source incident**, ARCH §10.1) → deploy → drift monitor → rollback |
| C15 | Knowledge capture (case → KB draft) | L4 | T3 | v0.2 | Resolved case becomes a draft KB article (gated) |
| C16 | Shadow mode & trust ramp | — | — | v0.0 (shadow run) / v0.1 (promotion gate) | Shadow → assisted → standard, with **measured promotion** (shadow = counterfactual baseline, ARCH §8.4); promotion to assisted is the **partner's decision** |
| C17 | Simulate / dry-run preview | L2 | T0 | v0.2 | Show predicted effects before asking for approval |
| C18 | PII/secret redaction (INV-06) | cross | T0 | v0.0/v0.1 | Strips detector-positive secrets/PII **inside `LLMClient.structured()`** — the only provider path, so no call site (classify, entailment, rerank) can skip it; the canary suite proves the path is wired in, and detector **recall** on a held-out realistic-secret corpus is a tracked quality metric (ARCH §6.1, §19). *Outbound-comms DLP is C4, not C18* |
| C19 | Bidirectional sync + human takeover | L2 | T0/T1 | v0.1 | ServiceNow update events drive case lifecycle; `human_owned` hard-stops agent sends/proposals |
| C20 | Alert-storm handling | L1 | T0 | v0.1 | Deterministic `case_groups` linking (catalog service / dependency edge / shared source; no LLM; one triage + one RCA per group); LLM-spend circuit breaker (ARCH §4.3, §11) |
| C21 | Approver RBAC + no-self-approval | cross | T3-policy | v0.0 (OIDC identity + no-self-approval) / v0.1 (roles); **two-person rule v0.2** | Roles per action×service×risk; approver cannot approve an action they created; **two-person rule for high-risk actions (v0.2)**; ticket-comment approval is not a valid channel |
| C22 | Value baselines (MTTA / first-update / MTTR) | L4 | T0 | v0.1 (shadow) | **Two separate, mode-recorded outputs:** *counterfactual* (shadow: alert→first-proposal, proposal correctness) and *assisted delta* (live: **treatment-vs-control** MTTA/TTFU/MTTR over the same period). In shadow, MTTA/MTTR delta is **zero by construction** (ARCH §8.4) |

---

## 4. Deep dives

### 4.1 Comms engine (C4) + duty loop (C5)

A comms plan is **data**, not a side effect — it's attached to the case and evaluated by the duty loop:

```yaml
comms_plan:
  - audience: internal (team channel / ServiceNow)     # tier T1
    triggers: [state_change:classified, state_change:progress, state_change:resolved]
    min_interval: null
    template: internal_status
  - audience: customer (email)                          # tier T2 — ALWAYS gated
    triggers: [state_change:classified, state_change:progress, state_change:resolved]
    min_interval: 4h                                    # "regular updates" even without state change
    template: customer_incident
    sla_deadline_trigger: true
```

- **One send path:** every send, internal or customer, is an **outbox row** authorized by an approval, a grant (internal kinds only), or an approved comms plan (customer-comms kinds only). `comms` is the history record (ARCH §6.3).
- **Send idempotency:** key = `(case_id, audience, trigger, interval_slot)`, `UNIQUE` in the outbox → a retry never double-posts. The honest guarantee is **at-least-once delivery + effectively-once effect** via read-before-write reconciliation against the target (ARCH §6.2) — not "structurally impossible" (the crash window exists), so **0 double-sends is a *measured* soak result (INV-04), not an assumption**.
- **Template safety (field classification and field source):** customer-facing templates render **only allowlisted fields**, and every allowlisted field declares its **source**: `record_state` (read from the ServiceNow record **as last set by a human actor**, for example `state`) or `schedule` (for example `next_update_eta`). **No auto-sent field may come from agent inference**, so an LLM's belief that an incident is resolved can never auto-send "resolved" to a customer (ARCH §6.3). LLM free text may appear **only as a human-edited draft** with per-send approval, never rendered raw into a send.
- **Plan-level approval (anti-fatigue):** a plan is a proposal of kind `comms_plan`, approved through the normal approval path (RBAC, no-self-approval, hash-bound). Approve the **plan** once (audience + cadence + template + which fields may vary, each with its source); updates that change only allowlisted enum/status fields auto-send; **free-text** changes to customer comms are gated individually. Approve-time and rubber-stamp rate are v0.1 metrics — fatigue is *measured*, not assumed (ARCH §6.3).
- **Outbound DLP (C4, comms engine — *not* C18):** in v0.1, customer-facing sends are restricted to the plan's `allowed_fields` (field-allowlist DLP). Pattern-based secret/PII scanning of outbound bodies is v0.2. C18 (INV-06) is a different mechanism: redaction of **LLM-bound** payloads inside the client (§6.1).
- **Duty loop schedule:** every N minutes (prov.), for each active case: post due min-interval updates (under `human_owned`, render them as **drafts for the owner** instead of sending, ARCH §8.2), ping the owner when progress is stale (>4h, configurable), raise SLA countdown, and at close (v0.2) draft the post-incident review and schedule the 24h post-resolution follow-up.
- **Comms history** is part of the case record and the trace; `supportagent stats` reports comms debt (updates owed but not sent, and why).

### 4.2 Job re-run decisions (C6) — the guardrail, resolved

The agent **decides** (proposes); the customer's **policy** (allowlist) may pre-approve; execution **always** flows through gate + outbox. A `job_rerun` proposal is only *valid* when it carries:

```json
{
  "kind": "job_rerun",
  "system": "airflow",
  "target": "dag:nightly-sync",
  "evidence": {
    "failed_run_id": "run-2025-10-16-003",
    "error_signature": "ConnectionError: upstream DB pool exhausted (3 consecutive runs)",
    "log_refs": ["tr:8842/log-17", "tr:8842/log-18"],
    "similar_case": "case-0191 (approved re-run 2025-09-02, resolved)",
    "recent_change_check": "no deploys in last 24h affecting this path"
  },
  "predicted_effects": ["reprocesses ~40k events, ~25 min, idempotent upserts"],
  "rollback_path": "re-run is idempotent; no manual rollback required"
}
```

- **Evidence discipline:** missing `error_signature` or `failed_run_id` → the proposal is **rejected by validation**, not by a human. This is graceful-failure applied to the agent's own ambition.
- **Simulate/dry-run (C17):** where the system allows read-only previews (GitHub Actions run state, Airflow task history), the approval screen shows predicted effects + blast radius + rollback path before the approver clicks anything.
- **Idempotency:** rerun key = `(target, failed_run_id)` → retrying the same failed run is a no-op. Re-running an *already-succeeded* run is rejected.
- **First-party rerun surfaces:** Airflow (task clear / DAG re-run), GitHub Actions (`re-run failed jobs` only as a distinct, allowlist-friendly scope). **`cron` re-invoke is deferred** — it is a code-execution capability and comes back only with a specified constrained runner (ARCH §7).

### 4.3 Root cause analysis (C2/C3)

A **budgeted hypothesis–evidence loop**, not a chat:

```
hypotheses H1..Hn  →  per hypothesis: evidence queries (logs, job runs, change feed, flags,
                     metrics/APM, GHA runs, ServiceNow changes)  →  MACHINE-checkable predicate
                     →  outcome: SUPPORTED | CONSISTENT_UNCONFIRMED | INCONCLUSIVE (categorical)
budget:  lite ≤ 5 tool calls / ≤ 3k tokens   ·   deep ≤ 25 calls (configurable)
```

- **Evidence discipline:** every claim in the RCA artifact links an evidence ID; unlinked claims are auto-tagged `speculation` and **excluded from customer-facing comms**. And a citation must *support* its claim: an entailment check (`claim.supported_by(evidence)`) gates customer comms, so "a citation exists" ≠ "the citation supports it" (ARCH §5.4). These rules are what make RCA trustworthy instead of plausible.
- **Categorical outcomes, not scalar confidence:** the artifact carries `outcome ∈ {SUPPORTED, CONSISTENT_UNCONFIRMED, INCONCLUSIVE}`; `score` is a derived, display-only value and never the decision variable. `SUPPORTED` means evidence *consistent with* the hypothesis exists, not proof of cause. **RCA outcomes never drive customer comms in v0.x**: runbooks may only declare internal `comms_if_supported` (ARCH §5). Loop termination uses **machine-evaluated predicates** over structured tool output, not an LLM reading prose `expected_evidence` (ARCH §5).
- **Hypothesis prior — set by data, not vibes:** hypotheses are generated with change-induced causes first (recent binary push, config push, feature-flag flip), because that is where the published real data puts them: Google's postmortem analysis of thousands of incidents (SRE Workbook, Appendix C, 2010–2017) shows **binary push 37% + configuration push 31% ≈ 68% of outage triggers** are change-induced (root causes: software 41%, dev-process failure 20%, complex system behavior 17%) — cited as a factual reference, not redistributed. Two honest corrections: (1) the prior is a **per-deployment parameter** and is **down-weighted unless its evidence sources are registered** (change feed/CMDB, feature-flag source, metrics/APM) — we do not rank "config push" first if we cannot test it (ARCH §5.5); (2) the **eval mix is broad and realistic, *not* weighted to the prior under test** — that would be train-on-test and would make the prior unfalsifiable (ARCH §11).
- **RCA artifact** (attached to the case, feeds case memory + KB drafts):
  ```json
  {"hypotheses": [...], "evidence_refs": ["tr:.../log-17"], "conclusion": "...",
   "outcome": "SUPPORTED", "score": 0.82 (display only),
   "recommended_actions": [{"kind": "job_rerun", ...}]}
  ```
- **Correlation (C3, v0.2):** shared resource + shared recent change + overlapping time window ⇒ link incidents, run one deep RCA, update all linked cases (each still individually gated).
- **First-party evidence feeds:** log files (error signatures), Airflow (failed task + run history), GitHub Actions (failed step, rerun count), ServiceNow (recent changes/cases) — **plus the prior-relevant sources:** deploy/config change feed (Argo CD / Terraform / Consul / CMDB), feature-flag source, metrics/APM (read-only). Without them, the change-induced prior is down-weighted (above).

### 4.4 Extension platform (C9/C10) + first-party tool set

**Registration contract (config, not runtime discovery):**
```yaml
tools:
  - name: airflow
    mcp: {endpoint: "https://tools.internal/airflow-mcp"}
    version: 1.4.0
    tools:
      - {name: status,   class: read,               tier: T0}
      - {name: history,  class: read,               tier: T0}
      - {name: rerun,    class: state-changing,     tier: T2, allowed_scopes: [dag:nightly-sync]}
  - name: logs
    mcp: {command: "supportagent-mcp logs --allow /var/log/prod,/srv/app/logs"}
    tools:
      - {name: read, class: read, tier: T0}
```

- **Unknown tool ⇒ denied.** A server can expose more than the config allows; only declared tools are callable.
- **Trust classes:** `read` / `execute` / `state-changing`. Class + tier are set at registration and enforced by the executor, not the prompt.
- **Injection safety:** all tool output wrapped as untrusted content; the eval suite includes injection canary cases (tool returns "ignore previous instructions…") that must stay green.
- **Onboarding a new tool (hardened in v0.3):** propose → trust review → ≥3 eval cases incl. failure modes → gate → human approval → enable.

**First-party tool set (reference deployment):**

| Tool | Reads (T0) | State-changing (T2) | Notes |
|---|---|---|---|
| `logs` | read files (allowlisted prefixes), pattern/grep, time range | — | path allowlist is config; outside = denied |
| `airflow` | DAG/task state, run history, task logs | rerun (task clear / DAG) | rerun idempotent per `(dag, failed_run)` |
| `cron` | job list, last run, exit codes, output tail | re-invoke (**deferred** — constrained runner required) | read-only in v0.1/v0.2 |
| `github_actions` | workflow run state, failed step logs | re-run **failed jobs only** (distinct scope) | `rerun_failed_only` is the allowlist-friendly scope |
| `servicenow` | incident/change records | comments, field updates | see ROADMAP "ServiceNow adapter notes" |

### 4.5 Service requests (C7) — both modes

**Intent routing** (L1): `incident | service_request | question`. Questions get a KB-grounded answer (T0), no case. Everything else opens a case.

- **Mode A — catalog mapping:** customer config maps their ServiceNow catalog items to fulfillment playbooks: `{catalog_item → params, handler (MCP tool | webhook), gate_tier, sla}`.
- **Mode B — generic templates:** agent-defined request types the customer instantiates: `{name: "password_reset", params_schema: {user}, handler, sla}`.
- **Flow:** classify → resolve request type → **collect params** (a missing param ⇒ a `customer_comment` asking the requester, never a guess. That comment is **customer-facing, so it is T2**: either a per-send approval, or an approved comms plan whose template is the catalog item's fixed parameter question and whose only varying field is the missing parameter's *name* from the catalog schema. It is never agent free text) → fulfillment proposal (T2; low-risk items may be allowlisted) → outbox execution → confirmation comms → SLA tracking → outcome into case memory ("what resolved this request type before").

### 4.6 The learning loop (C11–C15) — self-evolution, gated

**Feedback is captured at three moments** (v0.1, all stored with `trace_id`):
```
Feedback(case_id, trace_id, kind, actor, ts, payload)
  kind: approval   → {approved, reason, proposal_id}
  kind: diff       → {proposed_actions, actual_human_actions}     ← richest signal
  kind: outcome    → {resolved, reopened, diagnosis_correct, csat, comment}
```

**Ladder (release-ordered by design):**

| Substrate | Release | Mechanism | Safety |
|---|---|---|---|
| **Case memory** | v0.1 | Store resolved cases `{fingerprint, classification, approved_actions, resolution, comms_sent}`; retrieve top-k at triage; proposals that rely on a similar case **must cite its ID** | Context only — no behavior change |
| **Runbook distillation** | v0.2 | Miner finds action sequences with ≥N recurrences + consistent outcomes → drafts a **runbook (YAML)** (when/what/evidence requirements) → **full eval gate including a holdout disjoint from the training set in time AND signature** (train-on-test guardrail, ARCH §10.1) → human approval → registered | T3: gated + eval-gated + **holdout-disjoint check** + rollback |
| **Prompt/KB evolution** | v0.3 (research track, **in scope — not a v1.0 headline**) | Feedback-pattern mining → candidate prompt/KB **diff** → run **entire eval suite** on patched config (holdout disjoint from training in time AND signature) → per-case diff report → human approval → versioned deploy → **drift monitor** (live shadow scoring) → one-command rollback | T3: the strictest gate in the system |

**Self-evolution pipeline (T3-caged; kept because it is a stated requirement, but honestly ranked *behind* C12/C15 case memory, design-partner integration, sync, and storm handling: it is the riskiest, lowest-proven-value, most Goodhart-prone capability in the system):**
```
feedback → signal mining → candidate (skill doc | prompt patch | KB article)
   → EVAL GATE (full suite; holdout disjoint from training in TIME AND SIGNATURE — never cases generated from the source incidents, that is train-on-test)
   → human approval (with per-case diff report)
   → deploy (versioned) → drift monitor → rollback (one command)
```

**The non-learnable surface (sacred, T3-human-only):** the approval gate, the autonomy tier model, tool trust classes, the approval policy, RBAC, the eval-suite structure, **the routing table, and the classification/hypothesis taxonomy** (ARCH §10, §12). The agent may only *propose* changes to the learnable surface (prompts, KB, runbooks); the same eval gate + a human review every change. It can never learn its way out of the cage.

### 4.7 Shadow mode & trust ramp (C16)

| Mode | Agent may | Measured |
|---|---|---|
| **shadow** | T0 only; proposals visible but nothing executes | **counterfactual** (C22): alert→first-proposal time, proposal correctness vs. human's actual actions, triage agreement, false-escalation rate. *MTTA/MTTR delta is **zero by construction** in shadow — the agent never acts (ARCH §8.4)* |
| **assisted** | T0 + T1; T2 gated per-instance | **assisted delta** (C22): MTTA / time-to-first-update / MTTR vs. human-only baseline — the live customer-value number; plus approval rates, diff frequency, comms quality (CSAT) |
| **standard** | T2 grants active where the customer defined them | audit report: every auto-execution traceable to its `policy_grants` row |

- Promotion shadow → assisted is a **measured gate**: the **Wilson 95% lower bound** of triage agreement ≥ 0.75 (prov.), per severity band for P1/P2, over ≥ 60 (prov.) shadow cases. That is the same lower-bound rule as the release gates (ARCH §11). An observed 80% over 30 cases (lower bound ≈ 0.63) does **not** pass. Trust is *earned with data*, not configured, and the final promotion is the partner's decision (ARCH §8.4).
- Shadow mode is how "never auto-remediates" is sold: not a limitation, but **verifiable restraint**.

---

## 5. Release map

| Release | Capabilities | Exit criteria (measurable) |
|---|---|---|
| **v0.0 — Walking skeleton** (wk 1–4) | 1 adapter (ServiceNow, poll sync), service catalog, 1 routing row, gate + outbox (all INV-01 triggers) + trace + 1 LLM slot, **OIDC login + `users` + no-self-approval** (C21 core), first-run T1 grant setup, C18 (redaction-in-LLMClient), C16 (shadow mode) | **INV-01..INV-06 tests green on both engines against the design partner's real tickets** (incl. the INV-06 canary suite); shadow run on the partner's last 50 incidents, **which sets the provisional thresholds**; stranger-onboarding <15 min **on a ServiceNow developer instance** |
| **v0.1 — Trustworthy core** (wk 5–12) | C1, C2-lite, C4, C5-basic, C8, C10-static, C11, C12, C16, C18–C22 (redaction, sync/takeover, storm, RBAC, value baselines) | **INV-01..INV-06 green in CI (both engines)**; quality gates per ARCH §11 on a **150–250-case holdout** (thresholds provisional, re-set from v0.0 data); shadow promotion gate passed on the **Wilson lower bound** (§4.7); **0 duplicate cases and 0 double-sends in the 1,000-alert storm soak**; `human_owned` takeover demonstrably stops agent sends; **eval corpus: design-partner incident history primary, ≥50% source-attributed (partner data + status pages), licensing-clean (no verbatim redistribution of CC BY-NC-ND text)**; **shadow counterfactual baseline recorded (C22 — MTTA/MTTR delta is zero by construction in shadow; ARCH §8.4)**; assisted delta (live MTTA/TTFU/MTTR vs human-only) measured from v0.2 |
| **v0.2 — Support analyst with hands** (≈wk 18) | C6, C7, C3, C2-deep (+ feature-flag and metrics/APM evidence sources), C9 (MCP, now that a customer needs it), C13, C15, C5-PIR/follow-up, C17, **C21 (two-person rule full), C4 (pattern DLP beyond the field allowlist), Litestream PITR (named + tested)**, assisted mode with treatment/control (C22) | evidenced re-run proposal demo (Airflow + GHA); end-to-end service request (catalog + template); ≥1 runbook distilled, **holdout-disjoint (time AND signature) eval gate** green, approved; correlated multi-incident RCA demo; 1 real MCP server integrated; two-person approval on a high-risk action; PITR restore from a point before a corruption, verified in CI |
| **v0.3 — Safe self-improvement = v1.0** (wk 19–24) | C14 (research track, in scope, **not the v1.0 headline**), hardened tool onboarding, audit/compliance pack, multi-tenant **operation** (tenant_id in schema since v0.0), Helm | **v1.0 exit story = the value baseline (C22 assisted treatment-vs-control delta) + INV-01..06 green + full audit trail**; C14 is optional and not an exit criterion: self-change (prompt patch from feedback) passes eval gate **incl. time+signature holdout**, deploys versioned, rolls back in one command; injection canary suite green (incl. **inbound-ticket-body canaries**); full audit report from any trace |

**Deferred out of v0.1 (by decision):** feature-flag and metrics/APM evidence sources (→ v0.2; v0.1 ships exactly one change source the partner already has), cross-encoder reranker (→ v0.2), MCP (→ v0.2), Helm/K8s (→ v0.3), multi-tenant *operation* (schema tenant-ready from v0.0, operation in v0.3), full comms matrix (→ v0.2), **two-person rule (C21 full, → v0.2), outbound DLP beyond the plan-level field allowlist (C4, → v0.2), Litestream PITR as a named capability (→ v0.2)**, graph *index* (normalized signatures ship in v0.1; full graph index in v0.2), `cron` re-invoke (needs a constrained runner).

**Hard dependencies (project risks, not capabilities):** (1) a **design partner with ≥6 months of historical incidents** (tickets + alerts + logs + resolutions), requested on day 0. Without it, the eval corpus is real labels on synthesized inputs and the v0.1 exit criteria are unmet. (2) The **partner's decision to enable assisted mode** on a treatment set, which the C22 value number requires (v0.2). (3) An **external paging integration** for every catalog service (ARCH §14).

---

## 6. Open items (need a first-customer answer)

1. **Log source shape:** mounted files vs. S3 vs. Loki — decides the `logs` tool transport (allowlist model unchanged).
2. **Airflow:** version + whether the REST API is exposed (vs. scraping the UI — we'd refuse).
3. **GitHub Actions:** public cloud vs. self-hosted runners (affects re-run auth).
4. **SSO for approvals:** Okta / Entra / ServiceNow-native — picks the admin-UI auth path (RBAC roles per ARCH §6.4).
5. **Customer comms channel for the reference deployment:** email, ServiceNow, or Slack first.
6. **Design-partner data access (blocking):** shape/retention of the historical ticket + alert + log archive; export path; NDA/licensing terms. This gates the v0.1 eval corpus.
7. **RCA evidence sources:** which **one** change source (Argo CD / Terraform / Consul / ServiceNow change records) does the partner already have for v0.1? Which feature-flag store and metrics/APM system for v0.2? The prior's weight follows this answer (ARCH §5.5).
8. **SLA calendar / maintenance windows / change freezes:** where do we read them? Needed for prior down-weighting (ARCH §5.5).
9. **Assignment model and automation actors:** which assignment rules, predictive-intelligence or flow users set `assigned_to`? (`doctor --discover-automation`; ARCH §8.2.) This decides what counts as human takeover.
10. **Paging integration:** which external pager covers each catalog service? Required before triage starts (ARCH §14).
11. **`sys_audit` retention and REST rate-limit headroom** on the instance: these decide the sync design's loss bounds and the poll interval (ARCH §8.1).
