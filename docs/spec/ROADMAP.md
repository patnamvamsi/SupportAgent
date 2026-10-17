# SupportAgent — Build Roadmap

**What it is:** a deployable, config-driven "application support analyst." A team deploys one container, points it at their ticketing/alerting systems, and it triages alerts, manages ticket lifecycle, and drafts communications — **proposing actions; state-changing and customer-facing actions never execute without a human approval or a customer-owned policy entry.**

**The product promise (non-negotiable — and each one now maps to a testable invariant, ARCH §2):**
1. Evals on every change, regression gate in CI — as **INV-01..INV-06** (hard) plus a quality suite (distribution).
2. Guardrails: **T2-and-above is propose → human approves → execute; T1-internal actions are policy-allowed by default** — every execution traces to a human or a customer-owned, audited policy entry, enforced at the DB layer. No config that turns the gate off.
3. Observability: every tool call, token, and decision logged with trace IDs; any triage decision replayable (INV-03).
4. Idempotency & dedup: the same alert (in window) becomes **one case**; retries are **effectively-once** (at-least-once delivery + reconciliation) — 0 double-acts is a *measured* soak result (INV-02/INV-04).
5. Graceful failure: on hallucination/timeout, degrade to a safe default — never crash, never guess (INV-05).

**Positioning (one line):** SupportAgent is not another "AI copilot chatbox" — it is a **deterministic support pipeline with LLM slots**. The state machine *is* the product; the LLM is a typed, budgeted component inside it. Against "just add an AI assistant to ServiceNow," we win on replayability, auditability, and the 3 a.m. story.

**Non-goals (v0.x, by choice — full list in ARCH §13):** not a pager (we `enrich_page` an existing page, and page creation is not implemented in v0.x; an external paging integration is a deployment prerequisite — ARCH §14); not an auto-remediator; not a general chatbot; not multi-tenant-*operated* in v0.1 (schema is tenant-ready from day one); no unattended self-modification of the control plane.

**Capability model:** see [CAPABILITIES.md](CAPABILITIES.md) — autonomy tier model, capability registry (C1–C22), comms engine, job re-run policy, learning loop, release map. **Technical architecture:** see [ARCHITECTURE.md](ARCHITECTURE.md) — the **normative** spec: invariants (INV-01..06), taxonomy, components, the two loops, routing engine, runbooks (YAML) + retrieval, gate/outbox/comms (real enforcement), data model, capability→component map. This roadmap says *when*; the other two say *what* and *how*. Where they disagree, ARCHITECTURE wins.

---

## 1. Architecture (framework-agnostic core)

```
Inbound adapters (ServiceNow — first; generic webhook; Zendesk lands in v1.0 — the diagram
only shows what is actually scheduled)
      │
      ▼
Ingest ──► AlertEvent (typed, canonical, tenant-scoped)
      │
      ▼
Dedup + storm grouping ──► fingerprint (alerts: normalized signature; tickets: identity)
                          + one unresolved case per fingerprint (DB index)
                          + deterministic case_groups linking (no LLM; C20)
      │
      ▼
Classify ──► pre-classifier ‖ LLM structured output ──► schema + conflict check
              (safe default on failure — INV-05)
      │
      ▼
Propose ──► typed actions (T2+ gated; T1-internal policy-allowed)
      │
      ▼
ApprovalGate ──► pending → approved / rejected / expired (RBAC, no self-approval; C21)
      │
      ▼
Outbox ──► idempotent + read-before-write reconciled execution (ServiceNow, email; Slack in v0.2)
      │
      ▼
Comms (plan-level approval, cadence; every send is an outbox row)  ·  Sync (ServiceNow
poll + sys_audit history → case lifecycle; `human_owned` hard-stops agent sends — C19)
      │
      ▼
Loop B: RCA (budgeted, runbook-as-data, machine-checkable evidence; ARCH §5)

Cross-cutting:  LLM client (OpenAI/Anthropic/Ollama; render → REDACT → provider, INV-06)
                event store (append-only, INV-03, canonical schema in ARCH §15)
                eval suite (invariants INV-01..06 + quality; CI gates)
                SQLite/Postgres + Alembic (dedup, outbox, approvals, tenant_id on every table)
```

**Key decisions (opinionated, with rationale):**

| Decision | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | Best ecosystem for eval harnesses, typed structured output, LLM SDKs. (If you prefer TS: Mastra or a custom loop — architecture is identical.) |
| Framework | **Thin explicit pipeline, no LangGraph/CrewAI** | The core loop is linear (5 stages), and 4 of the 5 pillars are *infrastructure* (dedup store, outbox, tracing, validation) that frameworks don't give you. A ~500 LOC orchestrator of typed stages is fully testable, replayable, and swappable. LangGraph's human-in-the-loop interrupts are nice, but they'd own your state machine — and the approval gate is the heart of the product. You can wrap the same stages in LangGraph later if you want graph features. |
| API/ingest | **FastAPI** | Webhook receiver + approval API + tiny admin UI (HTML/HTMX, no SPA build step). |
| Storage | **SQLite by default, Postgres via flag** | "Deploy one container, it just works." Single-container default; Postgres when you want multi-replica. One schema either way. |
| LLM access | **One thin `LLMClient` interface** (structured output + JSON schema + timeout) with OpenAI / Anthropic / Ollama backends | Provider-agnostic, air-gap friendly, trivial to swap or mock in evals. |
| Adapters | **InboundAdapter / OutboundAdapter interfaces** | Inbound: normalize vendor payload → canonical `AlertEvent`. Outbound: execute approved actions on ServiceNow/Zendesk/etc. **ServiceNow first** (most enterprise app-support runs on it), Zendesk second, generic webhook covers the rest. |
| Packaging | **One Docker image + `supportagent.yaml` + env secrets** | The deployment story: `docker compose up` (or single container). `supportagent doctor` validates config, connectivity, and auth before the first alert. |

### Canonical types (the contract everything else plugs into)

```python
AlertEvent(id, source, adapter, ts, fingerprint_fields, severity_hint?,
           subject, body, resource, ticket_ref?, raw)
TriageResult(case_id, classification, confidence, summary,
             proposed_actions: [Action], comms_draft?, needs_human: bool, reason?)
Action(id, kind, target, payload, risk: low|medium|high,
       idempotency_key, tier: T0|T1|T2|T3)   # tier assigned at registration, never inferred
       # T2+: gated (or customer-owned policy_grant); T1: internal, policy-allowed default
       # trust class and tier must agree — enforced by config validation at load (ARCH §7)
```

**ServiceNow adapter notes (first adapter):**
- **Onboarding:** `supportagent doctor` validates the instance URL + MID credentials (OAuth client credentials or basic auth) with a read-only probe against the `sn_custom_integration` scope — no writes before the first alert.
- **Inbound:** **poll-based** (ARCH §8.1): the `incident` table and, if Event Management is used, `em_alert`, each with its own overlap-windowed cursor, plus `sys_audit`/`sys_journal_field` history for actor-accurate change detection. **Fingerprint (ARCH §4.1):** alert-derived events use the normalized signature of `source + resource + alert_type + error_signature`; human-filed tickets use their identity (`number`), so two customers' tickets are never merged. Ten alerts for one outage make one case; related cases across resources are *linked* by storm grouping, not merged.
- **Bidirectional:** human changes (found in the per-change history, with automation actors excluded) drive the case lifecycle; `human_owned` hard-stops agent sends and proposals (C19, ARCH §8.2).
- **Outbound:** approved actions map to record operations: `work_note` (T1, `work_notes` field, **not visible to the caller**), `customer_comment` (T2, `comments` field, **visible to the caller**, never grant-authorized), `ticket_update`, `watch_list_add_internal` (T1) / `watch_list_add_customer` (T2), always through the idempotent outbox (action kinds: ARCH §12.2). `doctor` verifies the journal-field visibility on the partner's instance and the integration user's least-privilege role.
- **Throttling:** ServiceNow REST is rate-limited; the outbox executor enforces per-tenant QPS with backoff, and 429s are first-class retryable faults (never dropped, never retried unbounded).

---

## 2. The five pillars — where each is specified

This roadmap does **not** restate designs. Each pillar is specified once, in ARCHITECTURE.md. Below is only what this roadmap needs: the pointer, the release in which it lands, and the roadmap-specific material (the eval corpus plan).

| Pillar | Specified in | Invariant / gate | Lands |
|---|---|---|---|
| Evals: invariants vs quality, frozen snapshot, real-LLM prompt tests, Wilson gates sized to the suite | ARCH §11, §19 | INV-01..06 hard; quality gates (prov.) | invariants v0.0; quality gates v0.1 |
| Guardrails: proposals/approvals, INV-01 triggers (INSERT + UPDATE), single-use approvals, literal-scope grants, comms plans, RBAC, no self-approval | ARCH §6.2–§6.4 | INV-01 | v0.0 (two-person rule v0.2) |
| Observability: append-only events, canonical kinds, replay | ARCH §15 (kinds), §3 | INV-03 | v0.0 |
| Idempotency & dedup: unresolved-case unique index, upsert, outbox reconciliation, storm grouping | ARCH §4.1, §4.3, §6.2 | INV-02, INV-04 | dedup v0.0; storm grouping v0.1 |
| Graceful failure: per-stage safe defaults, circuit breakers, fault injection, spend breaker | ARCH §4, §11, §19 | INV-05 | v0.0 (fault-injection suite v0.1) |
| Redaction: inside `LLMClient`, canaries + held-out recall | ARCH §6.1 | INV-06 | v0.0 |

### 2.1 Eval corpus plan (roadmap-specific)

- **Primary corpus: the design partner's historical incident history** (tickets, alerts, logs, resolutions, ≥6 months). It is the only corpus with real *inputs*. It is a **hard dependency** (§5), requested on **day 0**.
- **Size target: a 150–250-case v0.1 holdout**, oversampling P1/P2 (≥ 60) and with ≥ 120 claim/evidence pairs for citation faithfulness. These sizes are what the provisional Wilson gates require (ARCH §11). The 25–50-case v0.0 set is a **smoke suite** for wiring and invariants, not a release gate.
- **Secondary, source-attributed:** GitHub Status (public JSON API; real public timelines, which are ground truth for `comms_contains`) and Atlassian status pages. These are post-hoc summaries: real *labels* on synthesized inputs, not real payloads.
- **Google SRE Book/Workbook:** the 68% change-induced statistic (Appendix C) is **cited as a fact only**. It is CC BY-NC-ND, so no text is redistributed or adapted and no eval cases are built from it.
- **Mix:** broad and realistic, **not** weighted to the change-induced prior under test (that would make the prior unfalsifiable). Synthetic cases cover only the adversarial edges: garbage-in, injection canaries (including inbound ticket bodies), timeouts, duplicates and storms.
- **Frozen snapshot:** every suite run pins a KB, case-memory and catalog snapshot (ARCH §11).
- **Rules:** eval cases live in the repo and are reviewed like code. A case can only be changed by adding a `meta: why` note, which prevents "fixing" the test to match the bug.

---

## 3. Phased delivery (v0.0 wk 1–4 → v0.1 wk 5–12 → v0.2 wk 13–18 → v1.0 wk 19–24)

> Each phase ends in a **demoable increment**. Capabilities (C1–C22) and their autonomy tiers are defined in [CAPABILITIES.md](CAPABILITIES.md) §3; the release map with exit criteria is there in §5.
> **Hard dependency (a project risk, not a capability):** a **design partner with ≥6 months of historical incidents** (tickets + alerts + logs + resolutions). The v0.0 shadow run and the v0.1 eval corpus depend on it. **Day 0 (not month 1): send the partner the data-access + legal request** — archive shape and retention, export path, who owns the corpus, licensing terms — while the Phase-0 foundations are being built.
> **Team size (assumption to confirm):** **2–3 engineers** (platform + LLM/evals + integration, product-owner part-time). The phase split above maps to that; a solo build doubles the calendar.

### v0.0 — Walking skeleton (Weeks 1–4)

**Phase 0 — Foundations (Week 1).** Repo (uv, ruff, pytest, pre-commit), Dockerfile, config schema + `supportagent doctor`, canonical types + **`core/kinds.py` action-kind enum (ARCH §12.2)**, DB schema (SQLite **and** Postgres + **Alembic migrations**, triggers in both dialects), CI skeleton running lint + tests on both engines. **Every test ID in ARCH §19 is committed as a failing test in week 1**; from then on, the invariant suite is the spec, and spec changes go through ADRs (`docs/adr/`) attached to code PRs. Doc-lint jobs (`test_docs_action_kinds_canonical`, `test_changelog_anchors_resolve`) run from day one.
**Exit:** `docker compose up` with a toy config runs `doctor` green; the ARCH §19 test list exists and is red.

**Phase 1 — The full trust chain on one real adapter (Weeks 2–4).** **ServiceNow inbound via overlap-windowed poll sync** (per-table cursors, `(sys_id, sys_mod_count)` dedup, `sys_audit` history for actors; ARCH §8.1) + **service catalog** (`services` rows: repos, log paths, DAGs, CMDB CI, on-call, owners, `depends_on`; ARCH §7) + **`doctor --discover-automation`** (automation-actor list, confirmed by a human; ARCH §8.2) → **redaction inside `LLMClient.structured()`** (INV-06 + canary suite; ARCH §6.1) → dedup (`ux_cases_unresolved` + upsert, human tickets fingerprinted by identity; INV-02) → classify (**pre-classifier ‖** LLM structured output; fail-closed `calibrated_score`; LLM-only P1/P2 severity cap; ARCH §4.2) → propose → **approval gate + outbox with all INV-01 triggers** (INSERT, UPDATE-immutability, single-use, literal-scope grants; ARCH §6.2) → store. **OIDC login + `users` + `trg_no_self_approval`** (v0.0 approvals need real identities; ARCH §6.4) and the **first-run setup screen** where the customer admin approves the default T1 grants. Full tracing (canonical kinds, ARCH §15) + `replay` CLI + token accounting. **INV-01..INV-06 green on both engines against the design partner's real tickets.**
**Exit demo:** the customer admin approves the default T1 grants at first run; fire a real partner alert → typed proposal; `work_note` auto-posts (T1, under that customer-approved grant) while the T2 action stays `pending` until an OIDC-authenticated approver (not its creator) approves it in the admin UI; `replay <trace>` shows the whole path; INV-01..06 green on both engines. **The shadow run on the partner's last 50 incidents produces the first values for every (prov.) threshold**, recorded in an ADR.

### v0.1 — Trustworthy core (Weeks 5–12)

**Phase 2 — Takeover + plan-level comms + roles (Weeks 5–7).** Case lifecycle from the per-change history (ARCH §8.1); **`human_owned` takeover** on **human-actor** changes only (automation actors excluded), with owner drafts, explicit hand-back and the `sync_takeover` event; per-change **echo filter** (ARCH §8.2). Comms engine: **`comms_plans` as approved proposals** (hash-bound), every send an outbox row, allowed fields with declared **sources** (`record_state` set by a human, or `schedule`; never agent inference), idempotent + reconciled sends (C4, ARCH §6.3). `customer_comment` (T2, caller-visible) vs `work_note` (T1). RBAC **roles** (viewer / analyst / approver / admin) on top of the v0.0 identity; the two-person rule is v0.2 (C21).
**Exit demo:** an assignment rule auto-assigns a new ticket → **not** takeover; a human then changes its state → `human_owned` → the agent stops proposing and sending, and shows a due cadence update as a draft for the owner (C19). One comms-plan approval covers the customer email cadence with exactly one send per slot. An LLM-inferred "resolved" never auto-sends; only a human-set ServiceNow state does. An approver cannot approve their own proposal.

**Phase 3 — Evals + CI regression gate (Weeks 6–8).** Grow from the v0.0 smoke suite (25–50 cases) to the **150–250-case v0.1 holdout** (≥ 60 P1/P2, ≥ 120 claim/evidence pairs; §2.1), drawn primarily from the partner corpus. **Frozen eval snapshot** (KB + case memory + catalog; ARCH §11). Invariants + quality split; **Wilson lower bounds computed over cases** (per-case majority over N samples); per-label `calibrated_score` table; held-out secret corpus for redaction recall; per-commit results, **CI regression gate** and trend report. All thresholds are (prov.), set from v0.0 data.
**Exit demo:** land a deliberately bad prompt change → the quality suite's lower bound drops below the provisional gate on the **real LLM**, with a per-case diff → revert → green; land a broken routing row → **invariants red**, merge blocked.

**Phase 4 — Resilience (Weeks 7–9).** Retries/backoff, circuit breakers, safe-default fallback table, degraded mode, fault-injection suite, model-deprecation check (ARCH §9), 1,000-alert storm soak as a CI gate.
**Exit demo:** LLM timeout + ServiceNow 500 injected → agent degrades to read-only, routes to humans, full trace of every fallback.

**Phase 5 — Case memory + RCA-lite + storm (Weeks 8–10).** Case memory (store + retrieval + **mandatory citation**, C12); feedback capture at the 3 moments from the per-change history (C11); **RCA-lite (C2-lite)**: runbook-as-plan, budgeted, `check:` compiled as CEL with `$service.*` binding, `reduce()` truth table (SUPPORTED / CONSISTENT_UNCONFIRMED / INCONCLUSIVE), fixed hypothesis taxonomy, internal-only `comms_if_supported` (ARCH §5); static runbooks (C10); **storm grouping via `case_groups` (C20, ARCH §4.3) + LLM spend breaker**; **`enrich_page` (T1) and `page_recommended` banners** (no page creation; ARCH §14). First-party **read-only** tools: `logs`, `airflow.status/history`, `cron.status`, `gha.status`, `oncall`, plus **exactly one change source the partner already has** (CAP §6 item 7). Feature-flag and metrics/APM sources are **v0.2**. MCP is v0.2 (C9).
**Exit demo:** triage cites a similar past case by ID; RCA reads the logs inside the same trace and reduces to SUPPORTED / CONSISTENT_UNCONFIRMED / INCONCLUSIVE, with nothing about cause sent to the customer; a 1,000-alert storm across many fingerprints → 0 duplicate cases, LLM calls bounded by the number of groups, 0 double-sends (measured); INV-06 canaries green and detector recall reported; a log read and an Airflow status check appear inside the same trace.

**Phase 6 — Shadow counterfactual + v0.1 productization (Weeks 10–12).** Shadow = **counterfactual** value baseline: alert→first-proposal time, proposal correctness, triage agreement (MTTA/MTTR delta is **zero by construction** in shadow — the agent takes no action; ARCH §8.4); shadow → assisted trust ramp with agreement metrics (C16); `supportagent stats` (incl. the counterfactual baseline + approval-fatigue metrics); docs + `quickstart.md`; README with the **INV-01..06** guarantees. **Cut from v0.1 (by decision): two-person rule → v0.2; DLP beyond the plan-level field allowlist → v0.2; full comms matrix (Slack/Teams) → v0.2; MCP → v0.2; Litestream PITR as a named, tested capability → v0.2; Helm chart → v0.3; multi-tenant *operation* → v0.3 (the *schema* is tenant-ready from day one — `tenant_id` on every table).**
**Exit = v0.1:** a stranger can `docker compose up`, wire a ServiceNow **developer instance** + a tool endpoint, and see a triaged, **cited**, approved, replayable case in under 15 minutes. On the partner: quality gates pass on the 150–250-case holdout, and the **shadow promotion gate passes on the Wilson lower bound** (CAP §4.7), with zero double-sends. *(Onboarding a production instance takes longer: automation-actor discovery, journal-field visibility and the paging-integration check are part of `doctor`.)*

### v0.2 — Support analyst with hands (Weeks 13–18)

**Phase 7 — Job re-run decisions (Weeks 13–15).** `job_rerun` action type with mandatory evidence (failed run ID, error signature, similar-case citation); policy-grant semantics + UI; simulate/dry-run preview (C17); Airflow/GitHub Actions rerun surfaces (**`cron` re-invoke stays deferred** — it needs a constrained runner, ARCH §7); rerun idempotency keys.
**Phase 8 — Service requests (Weeks 14–16).** Intent routing (incident / request / question); ServiceNow catalog mapping **and** generic request templates; param collection via **`customer_comment` (T2: per-send approval, or an approved plan with the catalog's fixed parameter-question template)**, never a guess (CAP §4.5); fulfillment flow + SLA tracking (C7).
**Phase 9 — Deep RCA + correlation + evidence sources (Weeks 15–17).** Deep-budget hypothesis loop; cross-incident correlation; RCA artifact; KB drafts from resolved cases (C2-deep/C3/C15); **feature-flag and metrics/APM read-only sources** as the partner needs them; cross-encoder reranker.
**Phase 9b — MCP extension (Weeks 15–17).** MCP registration contract with trust classes (declared at registration, injection-safe) + integration of the first real server (C9 — deferred from v0.1 until a customer brought one).
**Phase 10 — Learning: runbooks + knowledge (Weeks 16–18).** Sequence miner → draft **runbooks (YAML)**; the distilled runbook is gated by the full eval suite **incl. a holdout disjoint from training in TIME AND SIGNATURE (train < T, holdout > T, no shared fingerprint — ARCH §10.1) — never cases generated from the source incident, that is train-on-test**; T3 pipeline (full eval gate → human approval → versioned deploy → one-command rollback) (C13).
**Phase 10b — Trust, durability and assisted mode (Weeks 16–18).** **Two-person rule for high-risk actions (C21 full, ARCH §6.4)**; **pattern-based outbound DLP beyond the field allowlist (C4)**; **Litestream PITR, named and tested in CI (ARCH §8.1)**; **assisted mode with a treatment/control split** agreed with the partner (C22, ARCH §8.4). Turning it on is the partner's decision (§5).
**Exit = v0.2 demo:** failed Airflow task → agent proposes an *evidenced* re-run → **customer-owned `policy_grants` entry** auto-executes it (traceable to that exact row — the promise holds: every execution traces to a human or a customer-owned policy) → customer update sent on cadence → the approved sequence is distilled into a runbook, **holdout-disjoint eval-gated**, and approved.

### v0.3 — Safe self-improvement (Weeks 19–24) = **v1.0**

**Phase 11 — Prompt/KB evolution (Weeks 19–21), optional.** C14 is a **research track**. It is in scope by requirement but is **not a v1.0 exit criterion**, and it is the first thing to slip if the schedule tightens. Pipeline: feedback → candidate patch → full eval gate **incl. the time + signature holdout** with per-case diff → approval → versioned deploy → drift monitor → one-command rollback.
**Phase 12 — Hardening (Weeks 22–24).** Hardened tool-onboarding workflow (trust review + required eval cases); prompt-injection canary suite (**incl. inbound-ticket-body canaries**); audit/compliance pack (any trace → full audit report); **multi-tenant operation** (tenant_id in schema since v0.0); **Helm chart**; second inbound adapter (Zendesk); v1.0 release.
**Exit = v1.0 demo (the value story):** the **assisted treatment-vs-control delta** in MTTA, time-to-first-customer-update and MTTR on the partner's services, with confidence intervals (C22, ARCH §8.4); INV-01..06 green on both engines; a full audit report generated from any trace. *If C14 shipped:* a prompt patch derived from feedback passes the holdout-gated eval, deploys versioned, and rolls back with one command. That is a bonus, not the headline.

---

## 4. Demo script (the 10-minute pitch)

1. **Triage (real incident):** fire the alert derived from a **real, public GitHub Status incident**, *"[Retroactive] Actions workflow run logs delayed"* (githubstatus.com, Oct 13, 2026; its full public timeline from investigating to resolved is the ground truth the eval case checks). The pre-classifier and the LLM agree, so the agent classifies P1, writes a case summary, and proposes: **`work_note`** (T1, auto-posts under the customer-approved setup grant, not visible to the caller), a **customer status email** (T2, gated), **`watch_list_add_customer`** (T2, gated), and **`enrich_page`** (T1: attaches context to the page the customer's own monitoring already opened; the agent never creates a page, ARCH §14). The work note is already posted, traceable to its grant row. The two T2 actions are pending.
2. **Gate:** open the admin UI as an OIDC-authenticated approver → approve the customer email, reject `watch_list_add_customer` (with a reason) → the ServiceNow incident receives exactly one internal work note plus one approved customer email. The rejection is logged with approver and trace ID. Try to approve a proposal you created yourself: the database refuses. **Every execution traces to a human approval or a customer-approved grant row; the INV-01 audit query proves it.**
3. **Dedup:** re-fire the same alert twice + inject a retry → counter ticks to 3, still one case, zero double-sends (soak-test numbers shown).
4. **Failure:** `SUPPORTAGENT_FAULT_INJECT=llm_timeout,sn_500` → agent degrades to read-only, human-queue message posted, no crash. Trace shows the exact fallback decision.
5. **Replay:** `supportagent replay <trace-id>` → the entire decision path, tokens, latencies, every tool call.
6. **Regression:** push a bad prompt change in a PR → the **quality suite drops below the pinned baseline on the real LLM** (prompt-hash cache invalidated → live run) with a per-case diff; push a broken routing row → **invariants red**. Both block the merge.
7. **Memory:** re-fire an alert similar to one resolved last month → triage cites the past case by ID and proposes the same actions a human approved then.
8. **Extension + restraint:** register the first-party read-only `logs` tool in config → RCA reads the logs inside the same trace; attempt to call an *undeclared* tool → denied, logged, traced. *Restraint is the product: the gate is infrastructure, not a prompt.*

**The closer:** see [PITCH.md](../PITCH.md) — the value story: counterfactual (shadow) and assisted-delta (live) reported separately, and the source-attributed corpus.

---

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Adapter sprawl (every ticketing system has quirks) | Strict canonical `AlertEvent` contract; adapters are thin and isolated; ServiceNow/Zendesk cover the long tail via the generic webhook adapter |
| LLM cost/latency at scale | Structured-output with tight max tokens; per-tenant model config; **invariants run against fixtures in CI (code only); the quality suite runs the real LLM in CI** (cached on **rendered-prompt hash + model ID + resolved version**, ARCH §11; N samples per case); nightly full re-run for trend |
| Prompt drift breaks behavior | Quality suite is a CI block (Wilson lower bounds over cases, real LLM, frozen snapshot, cache invalidated on any rendered-prompt or model change); invariants block on any code regression; prompts live in versioned files, not code strings. **Not every quality regression is caught**: the gate is statistical, and regressions inside the band can ship |
| Approval fatigue (humans rubber-stamp) | **Plan-level approval** (approve the plan once; fields sourced from human-set record state auto-send; free text gated) + per-action risk tiers + expiry; **measured** in `stats`: median approve time, rubber-stamp rate. Parameter questions for service requests are T2, so their approval load is budgeted (CAP §4.5) |
| Scope creep toward "auto-remediation" | Product rule: **T2-and-above always gated** (a human approval or a customer-approved, literal-scope `policy_grants` row); customer-facing kinds are **never grant-authorized** and `create_page` is **never executable** (literal kind lists in the INV-01 trigger, generated from `core/kinds.py`); the gate is **not configurable off**. Remediation is always someone else's (humans' or runbooks') job |
| Self-evolution destabilizes behavior | T3 rule: every learned artifact (runbook, prompt patch, KB article) passes the **full eval gate incl. a holdout disjoint from training in time AND signature (ARCH §10.1)** + human approval + versioned deploy + one-command rollback; the non-learnable surface (gate, tier model, tool trust classes, approval policy, RBAC, eval-suite structure, routing table, taxonomies, action-kind enum; ARCH §10/§12) is hardcoded; **C14 is optional and not a v1.0 exit criterion** |
| Extensibility becomes an injection vector | **The inbound ticket body is the primary injection surface**: treated as untrusted, redacted before any LLM call, and the catalog-based validator drops any action whose target does not resolve to the case's service; tool output is data, never instructions; trust classes fixed at registration; undeclared tools denied; injection canary cases in the eval suite (including inbound ticket bodies). An injected ticket cannot page anyone, because the agent cannot page (ARCH §14) |
| **Design-partner dependency (blocking)** | The partner's historical incident data gates the v0.1 eval corpus and the provisional-threshold ADR; tracked as the **top project risk** with a weekly checkpoint; the **day-0 data/legal request** (archive shape, export path, corpus ownership, licensing) goes out in week 1. Fallback ladder: **(a) live shadow traffic on the partner's production** (often faster through legal than a historical export), **(b) a second partner, (c) a re-planned schedule**. **Never** a relaxed exit criterion |
| **Assisted-mode decision (commercial)** | The C22 value number needs the partner to enable assisted mode on a treatment set while a control set stays human-only (ARCH §8.4). That is the partner's decision, not an engineering deliverable. Agree the treatment/control split and the decision date during v0.1; if the decision slips, v0.2 ships "assisted-ready" and the value number moves with it |
| **Paging becomes a critical path** | SupportAgent **never creates pages in v0.x**. The customer's own monitoring (PagerDuty / Opsgenie / ServiceNow on-call) is the only paging path, and `doctor` refuses to triage a service without one. `enrich_page` (T1) only attaches context to an existing page; a missing page raises a `page_recommended` banner for a human (ARCH §14) |
| **Alert storm (1,000 alerts in minutes)** | Dedup on unresolved cases + deterministic `case_groups` linking across services and dependency edges (no LLM; one triage and one RCA per group, ARCH §4.3) + LLM spend breaker; the 1,000-alert storm soak is a CI gate (0 duplicate cases, bounded LLM calls, 0 double-sends) |
| **Single-node durability** | The SQLite file is the system of record: Litestream continuous replication from v0.1, **PITR tested in CI in v0.2** (ARCH §8.1). Inbound is poll-based with an **overlap window and `(sys_id, sys_mod_count)` dedup**, so a record can be *delayed* by up to one poll interval but not missed; `sys_audit` retention bounds the history we can recover after long downtime (`doctor` reports it). Multi-replica scheduling uses DB leases |
| **Assignment automation looks like takeover** | Many instances auto-assign at creation. Takeover counts only **human actors** (not the integration user, not `system`, not the tenant's confirmed automation-actor list from `doctor --discover-automation`), evaluated on `sys_audit` history (ARCH §8.2). This is validated on the partner's instance in v0.0 |

## 6. Success metrics (v0.1)

- **Invariants (INV-01..INV-06): 100%, hard, on every PR** — zero invariant regressions shipped in the 12-week v0.0/v0.1 history.
- **Quality suite:** per-severity **Wilson lower bounds over cases** (per-case majority across N samples) at or above the provisional gates (ARCH §11) on the 150–250-case holdout; severity cost matrix (P1→P3 weighted far heavier than P3→P1); citation-faithfulness and redaction-recall rates reported; latency and cost per trace reported.
- Eval provenance: **design-partner incident history is the primary corpus**; ≥ 50% of cases source-attributed (partner data / GitHub Status / Atlassian status), each with a `source:` field; **licensing-clean: no SRE Book text redistributed or adapted**.
- 100% of side-effecting actions traced to exactly one valid authorization (an approval, a customer-approved grant, or an approved comms plan). Audit query: zero orphans. *The promise, as a query.*
- 1,000-alert storm soak: 0 duplicate cases, 0 double-executions, 0 double-sends (INV-02/INV-04, measured — not assumed).
- Graceful-failure suite: 100% of injected faults end in a safe default, 0 crashes (INV-05).
- Shadow promotion gate: **Wilson 95% lower bound of triage agreement ≥ 0.75 (prov.)**, per severity band for P1/P2, over ≥ 60 (prov.) shadow cases (CAP §4.7). An observed 80% over 30 cases does not pass.
- **Value baseline (C22), two separate `stats` outputs (ARCH §8.4):** **shadow = counterfactual** (alert→first-proposal time, proposal correctness, triage agreement; MTTA/MTTR delta is **zero by construction** because the agent takes no action), recorded at v0.1 exit. **Assisted = treatment-vs-control delta** (MTTA, time-to-first-customer-update, MTTR over the same period, with confidence intervals), from v0.2 once the partner enables assisted mode. The two are never conflated.
- **Approval fatigue (measured):** median approve time and rubber-stamp rate within agreed bands — fatigue is tracked in `stats`, not assumed away.
- Case memory: ≥ 50% of proposals on repeat-type alerts cite a prior case by ID.
- Time-to-first-triage-case on a **ServiceNow developer instance**: < 15 minutes via `supportagent doctor`. A production onboarding is measured separately: it includes automation-actor discovery, journal-field visibility checks and the paging-integration check.
