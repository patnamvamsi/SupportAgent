# SupportAgent — Review & Revision Log

> This file is the record of two critique rounds and how every suggestion was disposed of. It exists so the normative docs — [ARCHITECTURE.md](spec/ARCHITECTURE.md), [CAPABILITIES.md](spec/CAPABILITIES.md), [ROADMAP.md](spec/ROADMAP.md) — stay clean of review-response prose: they state *what is true now*; this file states *what changed, why, and where we pushed back*.
>
> - **Round 1:** [critique.md](reviews/critique-v1.md) (302 lines). Disposition: every substantive suggestion accepted, with two documented scoping adjustments (C13 placement, C14 scope).
> - **Round 2:** [critique-v2.md](reviews/critique-v2.md) (294 lines). Disposition: **accepted** 2.1–2.7, 3.1–3.3, 3.5, 3.6, 3.8–3.9, 4.1–4.5, 5.1–5.5, §6; **partially accepted** 3.4 and 3.7 (pushback rationale below); **no outright rejections.**
> - **Round 3:** [critique-v3.md](reviews/critique-v3.md). Disposition: **all ten top actions accepted.** Round 3 also found that several round-1/2 rows below claimed fixes the spec didn't contain. Those rows have been **corrected in place** (marked *R3-corrected*), and the spec now matches them. After round 3 the spec is **frozen**: further changes go through ADRs attached to code PRs (ARCH header).
>
> **Anchor rule:** every "where it landed" cell names an ARCH/CAP/ROADMAP section, and `test_changelog_anchors_resolve` (ARCH §19) fails CI if a named section doesn't exist.

Verdicts below cite the current location of the fix. "Accepted" = the critique was right and the doc now says what it said it should say. "Partial" = the mechanism proposed was kept because it prevents a worse failure mode; the critique's refinements were still incorporated.

---

## Round 1 — critique-v1.md

### §2 Internal contradictions

| # | Critique | Verdict | Where it landed |
|---|---|---|---|
| 1 | "No exceptions" to approval contradicted the T1-allowlist model | **Accepted.** Promise restated once: T2-and-above is always gated (human approval *or* customer-owned `policy_grants` entry); T1-internal is policy-allowed by default; every execution traces to one or the other, enforced at the DB layer; no config turns the gate off | ROADMAP L7 (pointer to this log), CAP §2 L39, ARCH §6.2 |
| 2 | ServiceNow fingerprint conflated ticket *identity* (`number`) with the dedup *signature* | **Accepted.** Identity = `number`; dedup = normalized signature of `source + resource + alert_type` (IDs/UUIDs/timestamps/hex/tenant constants stripped); partial unique index enforces one open case per fingerprint | ROADMAP L89, ARCH §4.1, INV-02 |
| 3 | `count` column referenced but absent from the schema | **Accepted.** Column is `duplicate_count` (ARCH §15); stale `count` references removed | ROADMAP L146 (pointer), ARCH §15 |
| 4 | Event kinds listed inconsistently across docs | **Accepted.** One canonical list of 19 kinds, owned by ARCH §15; ROADMAP/CAP only enumerate or reference | ARCH §15 |
| 5 | Multi-tenant/Helm claims exceeded the v0.1 scope | **Accepted.** Schema is tenant-ready from v0.0 (`tenant_id` on every table); *operation* deferred to v0.3 | CAP release map, ROADMAP phase 6 |
| 6 | CI gate mixed fixture tests with prompt-quality claims | **Accepted.** Two suites by design: invariants (deterministic code paths, single-sample, hard block) vs quality (real LLM, N samples, baseline band) | ROADMAP §2.1, ARCH §11, §19 |

### §3 Overclaims

| # | Critique | Verdict | Where it landed |
|---|---|---|---|
| 3.1 | Bad-prompt pathway: "the LLM picks the label, code maps the label to the path" was implied but not stated | **Accepted.** First principle made explicit (ARCH §1); deterministic **pre-classifier** cross-check (ARCH §4.2); self-reported `confidence` replaced by `calibrated_score` = **per-label empirical accuracy from the eval holdout, fail-closed (no entry → 0)**, plus an LLM-only P1/P2 severity cap; disagreement → safe default. *R3-corrected: this row previously said "N-sample agreement", which the spec never defined.* | ARCH §1, §4.2, §11 |
| 3.2 | RCA control flow let the LLM choose the next hypothesis, which is non-replayable | **Accepted.** Runbook = **code-owned step plan**; the loop iterates *steps*, not LLM-selected hypotheses; the LLM only fills per-step evidence and a categorical outcome; step checks are machine-verifiable | ARCH §5.1 |
| 3.3 | "Citation exists" was being treated as "citation supports the claim" (citation-wash) | **Accepted.** Entailment check (`claim.supported_by(evidence)`) gates customer comms; failing claims tagged `speculation` and excluded; citation-faithfulness rate is an eval metric with a release gate (≥0.95) | ARCH §5.4, §11 |
| 3.4 | "Exactly-once / structurally impossible double-sends" is not deliverable | **Accepted.** Honest restatement: **at-least-once delivery + effectively-once effect** via read-before-write reconciliation; "0 double-sends" is a *measured* 1,000-alert soak result, not a structural promise | CAP §4.1 L115, ARCH §6.2, ROADMAP §5 |
| 3.5 | Comms templates could leak arbitrary LLM text into customer emails | **Accepted.** Customer templates render only allowlisted enum/status fields (`status`, `next_update_eta`, human-approved text); LLM free text appears **only as a human-edited draft** in the approval UI, never rendered raw; per-plan `allowed_fields` | CAP §4.1, ARCH §6.3 |
| 3.6 | "Outbox is DB-enforced" was claimed on the strength of a `CHECK (x IS NOT NULL)` | **Accepted and superseded by round 2 §2.1** — a `CHECK` cannot see status/hash/grant validity. Now: FKs + `PRAGMA foreign_keys=ON` + `BEFORE INSERT` trigger `trg_outbox_inv01` (RAISE ABORT) + 4 targeted tests | ARCH §6.2, INV-01, §19 |
| 3.7 | Tool output as instruction channel | **Accepted.** Architectural defense first (the LLM never selects tools); the **primary** injection surface is the **inbound ticket body** — canary suite includes inbound-ticket-body canaries; deterministic validator requires any action target to relate to a case resource | CAP design rule 2, ARCH §7, §19 |
| 3.8 | A severity escalation (P3→P1) could be silently absorbed as a duplicate | **Accepted.** Dedup runs **first**; INV-02 enforced by schema (partial unique index + `ON CONFLICT` upsert), not code order; a material change (severity escalation, new resource, new alert_type) re-triages the open case **in place** (new `decision` event) — never a second open case, never absorbed | ROADMAP §2.4, ARCH §4.1, INV-02 |

### §4 Evals

| # | Critique | Verdict | Where it landed |
|---|---|---|---|
| 4.1 | Fixtures can't validate prompt quality; a bad prompt must be able to turn CI red | **Accepted.** Fixtures are for code tests only (routing, gate, outbox, dedup); prompt behavior runs the **real LLM**; cache keyed on **rendered-prompt hash + model ID + resolved version** — any prompt/model change invalidates the cache and forces a live run | ROADMAP §2.1, ARCH §11 |
| 4.2 | N≥30 band was stated but never used as a gate | **Accepted.** The shadow promotion gate is the **Wilson lower bound** (≥ 0.75 prov., ≥ 60 cases, per severity band for P1/P2). Quality gates use Wilson lower bounds over cases, and the holdout is **sized to the math** (150–250 cases, ≥ 60 P1/P2, ≥ 120 claim pairs). All thresholds are provisional, set from v0.0 data. *R3-corrected: the earlier gates were unpassable at the planned 25–50-case size.* | CAP §4.7, ARCH §11 |
| 4.3 | "Real incidents" claims needed honest provenance | **Accepted.** Corpus provenance is explicit: design partner's incident history **primary** (the only corpus with real *inputs*); GitHub Status (real incidents, full public timelines as ground truth); Atlassian status pages; SRE Book/Workbook as structural reference with the 68% change-induced stat **cited, not redistributed** | ROADMAP §2.1, CAP §5 |
| 4.4 | SRE Book is CC BY-NC-ND — redistribution is a real licensing risk | **Accepted.** NC is treated as a real risk: the 68% statistic is cited as a fact only. No SRE text is redistributed or adapted, and no eval cases are built from it. Licensing-cleanliness is a v0.1 exit criterion | ROADMAP §2.1, ARCH §11 |
| 4.5 | The 68% change-induced prior was presented as if it were ours | **Accepted.** The prior is a **per-deployment parameter**, down-weighted unless the evidence sources (change feed/CMDB, feature-flag store, metrics/APM) are registered — we don't rank "config push" first if we can't test it; the eval mix is broad and realistic, *not* weighted to the prior under test (that is train-on-test and would make the prior unfalsifiable) | CAP C2 row, ARCH §5.5, §11 |
| 4.6 | Missing eval dimensions (latency, cost, comms quality, per-provider) | **Accepted.** Eval dimensions now: classification/severity/actions (cost-matrix weighted), citation faithfulness, comms containment/judge, **latency (per-stage SLO)**, cost, per-provider behavior; graceful-failure suite is its own gate (INV-05) | ARCH §11, §19 |

### §5 Operations

| # | Critique | Verdict | Where it landed |
|---|---|---|---|
| 5.1 | The paging path was ambiguous — was the agent going to page? | **Accepted, then revised in round 3.** `enrich_page` (T1) attaches context to an *existing* page and never creates one. **`create_page` is not implemented in v0.x**: it is in `NEVER_EXECUTABLE_KINDS` and the INV-01 trigger forbids it. An external paging integration is a deployment prerequisite, and a missing page raises a `page_recommended` banner for a human. On-call lookup is a read tool. *R3-corrected: the earlier rule-only, gated `create_page` could not page at 3 a.m.* | ARCH §12.2, §14 |
| 5.2 | Per-message approval will produce rubber-stamping | **Accepted.** **Plan-level approval**: approve the plan once (audience + cadence + template + `allowed_fields`); enum/status-only updates auto-send; free-text changes gated individually; median approve time and rubber-stamp rate are v0.1 metrics in `stats` — fatigue is *measured*, not assumed | ARCH §6.3, CAP §4.1 |
| 5.3 | Alert storms would burn LLM spend and spawn case duplicates | **Accepted.** C20 = deterministic time+resource grouping (**no LLM**) + **LLM-spend circuit breaker** (safe-default classification, `enrich_page` attach-only, no new proposals); 1,000-alert soak: 0 duplicate cases, 0 double-sends, **measured** | CAP C20 row, ARCH §11, ROADMAP §5 |
| 5.4 | A customer commenting "approved" on their own ticket is a privilege-escalation path | **Accepted.** Ticket-comment approval is **not a valid channel** for state-changing actions; approvals go through the authenticated admin surface only, bound to the exact `payload_hash`; approver identity from OIDC SSO | ARCH §6.4, ROADMAP L132 |
| 5.5 | RBAC was a one-liner; no self-approval rule; grants lived in YAML free text | **Accepted.** `users`/`roles` tables; the DB is the single source of truth. YAML is a *request* (`supportagent grants request`) that writes `status='requested'` rows, which are **not enforceable** until an admin (≠ the requester, enforced by a schema CHECK) approves them. Scopes are **literal** (patterns are expanded at request time). No-self-approval ships in **v0.0** (`trg_no_self_approval`); the two-person rule is **v0.2**. Free-text `approved_by` and unanchored expiry are gone. *R3-corrected: the schema previously could not represent a pending grant.* | ARCH §6.2, §6.4, CAP §2 |
| 5.6 | Bidirectional sync would let the agent and the human overwrite each other; human takeover undefined | **Accepted.** Case-lifecycle state machine including `human_owned` (hard-stops agent sends and proposals; due cadence updates render as **owner drafts**); a per-change echo filter on `sys_audit` history; a narrow takeover definition (**human actor**, excluding the confirmed automation-actor list) with explicit hand-back. *R3-corrected: owner drafts and the automation-actor list are now in the spec.* | ARCH §8.1, §8.2 |
| 5.7 | Durability assumed vendor retries; no migration story | **Accepted.** Poll-cursor sync (`sys_updated_on > last_seen_cursor`) — no webhook-loss window; webhooks (where used) require HMAC/mTLS + replay protection + `webhook_rejected` audit event; DB-lease scheduling; Alembic migrations; **Litestream: ops item v0.1, *named + tested PITR capability* v0.2** (CI restore from a point before a corruption) | ARCH §7, §8.1, ROADMAP §2.7 |

### §6 Security & compliance

**Accepted, all of it.** Redaction of LLM-bound payloads is INV-06 and runs **inside `LLMClient.structured()`** — the only provider path — so no call site (classify, entailment, rerank, drafting) can skip it; canary suite proves absence (ARCH §6.1, §19). Inbound webhook auth made normative (HMAC/mTLS + replay protection, ARCH §7). Retention + **crypto-shredding** defined (ARCH §15). Tenant scoping from v0.0 enables tenant-scoped deletion.

### §7 Architecture

| # | Critique | Verdict | Where it landed |
|---|---|---|---|
| 7.1 | Pseudocode blocks were wrong in ways that matter | **Accepted** (round 2 found two more: dedup ordering, step iteration — fixed, see R2 2.5–2.6) | ARCH §4.1, §5.1 |
| 7.2 | Runbooks had no home: YAML without an executor, NL `expected_evidence` | **Accepted.** `runbooks/*.yaml` are executable step plans with machine-verifiable (CEL) predicates, schema-validated in CI; parameters are `$service.*` references, **bound at runtime** from the case's service catalog row. CI validates that the field names exist. *R3-corrected: this row previously said "at registration".* | ARCH §5.1, §7 |
| 7.3 | Retrieval was hand-waved | **Accepted.** Normalized signatures are dedup *and* retrieval keys; BM25 + LLM-judge reranker (cross-encoder reranker v0.2); retrieval is explicit and traced | ARCH §5.2, CAP C12 row |
| 7.4 | The learning loop allowed train-on-test | **Accepted.** Holdout disjoint from training in **time AND signature** (train < T, holdout > T, no shared fingerprint); **never cases generated from the source incident**; C14 behind the strictest gate | CAP design rule 4–5, ARCH §10.1 |
| 7.5 | Tier model had gaps (T1 "reversible" was not the criterion; T3 lumped artifacts and control) | **Accepted.** T1 redefined as *internal, low blast radius*; trust-class/tier agreement **fails config validation at load time**; T3 split into artifacts (less risk) vs control-plane changes (strictest path) | CAP §2 |
| 7.6 | Evidence sources were listed without saying which ones gate the prior | **Accepted, with a scoping adjustment:** v0.1 ships **exactly one change source the partner already has**; the flag source, metrics/APM and cross-encoder reranker land in v0.2. *R3-corrected: ROADMAP Phase 5 previously still listed all three families in v0.1.* | ARCH §7, ROADMAP §3 (Phase 5, Phase 9) |

### §8 Missing features (1–15)

Folded in where the critique was right; two honest deferrals:

| # | Critique | Verdict | Where it landed |
|---|---|---|---|
| 1 | Bidirectional sync + human takeover | **Accepted** → C19 | ARCH §8 |
| 2 | Redaction must be unskippable | **Accepted** → C18 / INV-06 inside `LLMClient.structured()` | ARCH §6.1 |
| 3 | Alert-storm handling | **Accepted** → C20 | CAP C20 |
| 4 | Approver RBAC / no-self-approval | **Accepted** → C21 | ARCH §6.4 |
| 5 | Maintenance windows / change freezes in paging + prior down-weighting | **Accepted** | ARCH §14, CAP §6 open item 8 |
| 6 | On-call schedule as a read tool, not a guess | **Accepted** | ARCH §7, §14 |
| 7 | Value baselines (MTTA/TTFU/MTTR) | **Accepted** → C22, two separate mode-recorded outputs | ARCH §8.4 |
| 8–9 | Evidence feeds (change/CMDB, flags, metrics/APM) | **Accepted, staged** (v0.1: one change source the partner has; v0.2: APM) | CAP C2 row |
| 10 | SLA calendar | **Accepted** → CAP §6 open item 8 | — |
| 11 | Slack/Teams approval buttons | **Accepted, v0.2** (comms matrix) | ROADMAP L132 |
| 12 | Ticket attachments/screenshots | **Deferred** — honest non-goal for v0.x; not claimed anywhere | — |
| 13 | Multilingual comms | **Deferred** — honest non-goal for v0.x; not claimed anywhere | — |
| 14 | Admin replay / audit reporting | **Accepted** → trace replay from v0.1; full audit/compliance pack v0.3 | ARCH §15, CAP v0.3 row |
| 15 | LLM-spend breaker | **Accepted** → part of C20 (ARCH §11) | CAP C20 row |

### §9 Scope / re-cut

**Accepted** — and refined by round 2 §4.5 (team size, dependency timing). Final cut: **v0.0 wk 1–4** (full trust chain on the ServiceNow adapter, on the design partner's real tickets) → **v0.1 wk 5–12** (triage, gated comms, RCA-lite, case memory, sync/takeover, RBAC, storm handling, shadow counterfactual baseline) → **v0.2 wk 13–18** (re-runs, service requests, deep RCA, MCP, runbooks, two-person rule, DLP+, PITR, comms matrix) → **v0.3 = v1.0 wk 19–24** (C14 research track, hardening, audit pack, multi-tenant operation, Helm).

### §10 Documentation

**Accepted.** This file and [PITCH.md](PITCH.md) carry review-response and positioning prose; ROADMAP §2 is a pointer table plus the eval-corpus plan; the normative docs own the canonical design; the demo script was fixed (ROADMAP §4); dangling cross-references fixed. *R3-corrected: ROADMAP §2 was not actually slimmed until round 3.*

---

## Round 2 — critique-v2.md

### §2 New defects (2.1–2.7) — all accepted

| # | Defect | Verdict | Fix (current location) |
|---|---|---|---|
| 2.1 | A `CHECK (x IS NOT NULL)` does not enforce INV-01 — it can't see the approval's status, hash, or the grant's validity | **Accepted.** This was the sharpest catch of the round | INV-01 restated to say exactly that; enforcement = `proposals`/`approvals` FKs + `PRAGMA foreign_keys=ON` (set on engine connect) + `BEFORE INSERT` trigger `trg_outbox_inv01` (RAISE ABORT unless `approved` + `payload_hash` matches, or in-scope unexpired grant) + 4 tests: `test_gate_rejected_approval_blocked`, `test_gate_expired_approval_blocked`, `test_gate_payload_hash_mismatch_blocked`, `test_gate_grant_expired_or_out_of_scope_blocked` (ARCH §6.2, INV-01, §19) |
| 2.2 | Customer comms could bypass the gate via a policy grant | **Accepted (completed in round 3).** Every send is an **outbox row**, so the INV-01 trigger covers comms. A comms plan is a proposal approved through `approvals`, with `plan_hash` re-checked on every plan-authorized send. Customer-facing kinds are rejected on the policy path by **literal lists in the trigger, generated from `core/kinds.py`**, not by a mutable table, and `create_page` is never executable. Auto-sent fields must come from human-set record state (ARCH §6.2, §6.3, §12.2) |
| 2.3 | Natural-language `check:` fields are an eval hole | **Accepted.** Checks are **CEL** (`cel-python`) — non-Turing-complete, deterministic; CI compiles every check against the tool-result schema; no `eval()` (ARCH §5.1, §16) |
| 2.4 | Redaction wrapped only `classify` | **Accepted.** Redaction moved **inside `LLMClient.structured()`** — the single provider path — covering classify, hypotheses, conclude, entailment, rerank, drafting; INV-06 canary suite (tokens planted in fixture ticket bodies/log lines never appear in any captured provider request) (ARCH §6.1, INV-06, §19) |
| 2.5 | Case created before dedup could break INV-02 under concurrency | **Accepted.** Pipeline order is **dedup-first**; INV-02 enforced by the schema (partial unique index + `ON CONFLICT` upsert), not by code order; `test_dedup_concurrent_no_second_case` (ARCH §4.1, INV-02) |
| 2.6 | RCA loop iterated hypotheses, not steps | **Accepted.** The loop iterates the **code-owned step plan**; the LLM only supplies per-step evidence and a categorical outcome (ARCH §5.1) |
| 2.7 | `$case.resource` was doing three jobs at once | **Accepted.** The service catalog row owns repos, log paths, DAGs, CMDB CI, on-call, owners and `depends_on`. Runbooks reference `$service.*`, **bound at runtime** (ARCH §5.1, §7) |

### §3 Consistency (3.1–3.9)

| # | Critique | Verdict | Fix / pushback |
|---|---|---|---|
| 3.1 | Broken cross-references | **Accepted** | 4 dangling refs fixed in round 1's follow-through; a further audit this round removed two more: a `§11.2` self-reference in ARCH §11 (§11 has no numbered subsections — the SLO is a plain bullet) and a `§13` pointer for the design-partner dependency (now points to CAP "Hard dependency" + ROADMAP risk section) |
| 3.2 | "Internal ticket comment" was customer-visible — the tier was mislabeled | **Accepted** | Split into `work_note` (T1, **not visible to the ticket caller**) and `customer_comment` (T2, visible, gated); ticket comments are never an approval channel (CAP §2, ROADMAP §2.2, ARCH §6.4) |
| 3.3 | Round 1 overcorrected paging — the LLM could still page via a grant | **Accepted, then revised in round 3** | `create_page` is **not implemented in v0.x** (forbidden by the trigger); `enrich_page` is T1 attach-only; external paging is a deployment prerequisite (ARCH §14) |
| 3.4 | "Conflict → safe default" will drown the human queue in a storm | **Partial.** | **Kept:** disagreement = we don't know = route to a human. That is INV-05, and the failure mode it prevents (silent LLM misrouting of a P1) is worse than the cost of a human queue slot; conflict volume is managed by making the rule set sharper (routing changes are reviewed PRs), not by weakening the conflict rule. **Accepted from the critique:** hierarchical **prefix agreement** (ARCH §4.2); `calibrated_score` as **per-label empirical accuracy from the holdout, fail-closed** (ARCH §4.2, §11; *R3-corrected*); **conflict/abstention rate as first-class telemetry** in `stats` (ARCH §11 quality suite) — so if the queue ever does drown, the metric shows it before it hurts |
| 3.5 | Material change → second open case violates INV-02 | **Accepted** | In-place re-triage: severity updated, classification + routing re-run on the same case row, new `decision` event appended (`retriage` is **not** a canonical kind — ARCH §15) (ROADMAP §2.4, ARCH §4.1) |
| 3.6 | Human takeover would trigger the agent's own self-echo | **Accepted** | Per-change echo filter on `sys_audit` history; takeover only by **human actors** (automation-actor list); explicit hand-back; cadence updates under `human_owned` render as owner drafts (ARCH §8.1–8.2) |
| 3.7 | C14 was "deprioritized" but still the v1.0 headline | **Partial.** | **Accepted:** the v1.0 exit story is the **value baseline (C22 assisted delta) + INV-01..06 green in CI + a full audit trail** — C14 is explicitly *not* the headline (CAP C14 row, release map v0.3 row); holdout is disjoint in **time AND signature**; **routing table + taxonomy** added to the non-learnable surface (CAP design rule 5, ARCH §10). **Pushback (documented):** C14 stays **in v0.3 scope** as a research track rather than moving to post-1.0. The roadmap commitment includes prompt/KB evolution in v0.3, and "in scope, not the headline" resolves the critique's actual concern (the v1.0 exit story) without deleting a product feature. If the eval gate proves too costly, C14 is the first thing that slips — the exit criteria do not depend on it. |
| 3.8 | "Shadow MTTR delta" is zero by construction — not a value story | **Accepted** | C22 restated as **two separate, mode-recorded `stats` outputs**: *counterfactual* (shadow — alert→first-proposal time, proposal correctness, triage agreement; MTTA/MTTR delta **zero by construction** because the agent takes no action) recorded at v0.1 exit; *assisted delta* (live MTTA / time-to-first-customer-update / MTTR vs human-only baseline) measured from v0.2 onward when the agent acts. The two are never conflated (ARCH §8.4, CAP C22 row) |
| 3.9 | YAML allowlist vs `policy_grants` = two sources of truth | **Accepted** | The **DB is the single source of truth**; YAML is a *request*; `supportagent grants request --from config` creates a **pending** `policy_grants` row, enforceable only after admin approval stamps `granted_by`/`granted_at_ts`/`valid_to_ts`; the outbox trigger matches against `policy_grants`, never YAML (CAP §2 L59, ARCH §6.4) |

### §4 Scope realism (4.1–4.5) — all accepted

| # | Critique | Verdict | Fix |
|---|---|---|---|
| 4.1 | v0.1 grew to include two-person rule, DLP, comms matrix — no longer a 10-week release | **Accepted** | Cut for real: two-person rule (→ v0.2), DLP beyond the plan-level field allowlist (→ v0.2), full comms matrix/Slack-Teams (→ v0.2), MCP (→ v0.2), Litestream PITR as a named capability (→ v0.2), Helm + multi-tenant *operation* (→ v0.3). v0.1 is now 8 weeks (wk 5–12) with an exit list that matches its phase list (CAP release map, ROADMAP phase 6) |
| 4.2 | v0.0's exit criteria depended on work scheduled later | **Accepted** | v0.0 exit (INV-01..06 green against the partner's real tickets, incl. the INV-06 canary suite; shadow run on the partner's last 50 incidents; stranger-onboarding <15 min) depends only on v0.0 work; the design-partner data request is a **day-0** project dependency, not a v0.0 phase item (ROADMAP §5 risks, CAP "Hard dependency") |
| 4.3 | The design-partner dependency starts in week 2 — too late for a legal/data negotiation | **Accepted** | Day-0 legal/data request; fallback order: live shadow traffic from the partner's current tooling → second design partner → replanned schedule. Tracked as the top project risk (ROADMAP §5) |
| 4.4 | Team size never stated, so the weeks are unreviewable | **Accepted** | **2–3 engineers** stated as an assumption in the schedule (ROADMAP §5); the release cut is defined against that number |
| 4.5 | Recommended cut: v0.0 = trust chain only; v0.1 = triage + comms + RCA-lite; v0.2 = hands; v0.3 = self-improvement | **Accepted with two documented deviations** | (a) **C13 runbook distillation stays v0.2** (the critique suggested post-1.0): case memory (v0.1) + C15 knowledge capture (v0.2) are the learning spine, and a holdout-gated C13 is the natural v0.2 exit demo — one or more distilled runbooks, eval gate green, approved. (b) **C14 stays in v0.3 scope**, not post-1.0 — see the 3.7 pushback above |

### §5 Still missing (5.1–5.5) — all accepted

| # | Gap | Verdict | Fix |
|---|---|---|---|
| 5.1 | The entailment check was asserted, never specified | **Accepted** | A separate LLM call, **judge-labeled** (not a boolean): `claim.supported_by(evidence)`; failure default = claim tagged `speculation`, excluded from customer comms; accuracy measured on a curated claim/evidence pair set and reported in `stats` (ARCH §5.4) |
| 5.2 | Prompt cache key was too coarse (prompt text only) | **Accepted** | Key = **rendered-prompt hash + model ID + resolved model version** (ARCH §11) |
| 5.3 | The quality "bands" don't actually gate anything | **Accepted** | Hard gates are the zero-tolerance invariants; quality gates are **Wilson lower bounds per severity on the holdout** (P1/P2 classification ≥0.90, severity ≥0.85, comms pass@3, citation faithfulness ≥0.95) with coverage floors (≥5 holdout cases per label, ≥50 total) — red = merge blocked (ARCH §11) |
| 5.4 | Durability still assumed the source retries everything | **Accepted** | Poll sync with an **overlap window and `(sys_id, sys_mod_count)` dedup** (*R3-corrected*: a strict `>` cursor drops same-second updates) means no webhook-loss window; webhooks (where used) require HMAC/mTLS + replay protection + a `webhook_rejected` audit event (ARCH §8.1, §7 inbound transport) |
| 5.5 | Round 1's missing list was still incomplete | **Accepted** | Retention + crypto-shredding (ARCH §15); triage p95 SLO with per-stage budgets (dedup <50ms, preclass <10ms, classify p95 <15s, RCA-lite p95 <60s) (ARCH §11); spend breaker (ARCH §11); `users`/`roles`/`proposals` tables (ARCH §15); webhook auth normative (ARCH §7) |

### §6 Documentation

**Accepted.** Review-response prose moved to this file; positioning/money-shot prose moved to [PITCH.md](PITCH.md); ROADMAP §2 slimmed to links + exit criteria (canonical design lives in ARCH/CAP); dangling xrefs fixed; the demo script rewritten against real, public incidents (ROADMAP §4).

### §7 Top-10 actions

All ten are reflected in the current docs — each maps to one of the rows above (2.1 INV-01 trigger; 2.2 comms gating; 2.4 redaction-in-client; 2.5/2.6 dedup order + step iteration; 3.8 two value outputs; 4.1 v0.1 cut; 5.2 cache key; 5.3 Wilson gates; §6 doc split).


---

## Round 3 — critique-v3.md

### Top-10 actions — all accepted

| # | Action | Fix (current location) |
|---|---|---|
| 1 | Close the INV-01 holes | `trg_outbox_immutable` (BEFORE UPDATE on payload and authorization columns); approvals are **single-use** (`UNIQUE outbox.approval_id`, trigger requires `p.status = 'approved'`, `trg_outbox_mark_executed` flips it); the executor re-hashes `payload_json` before sending; trigger also checks `p.payload_hash` and `p.action_kind` (ARCH §6.2) |
| 2 | Fix the grant schema | `policy_grants.status` (`requested`/`approved`/`rejected`/`revoked`), `requested_by`, nullable approval columns, CHECK `granted_by <> requested_by`; **literal scopes only**, with patterns expanded at request time (ARCH §6.2, §6.4; CAP §2) |
| 3 | Inline the kind lists, add `create_page`, one enum | `CUSTOMER_FACING_KINDS` and `NEVER_EXECUTABLE_KINDS` are **trigger literals generated from `core/kinds.py`**; the `customer_facing_kinds` table is removed; `create_page` is never executable; the canonical action-kind table, with `watch_list_add_internal` (T1) vs `watch_list_add_customer` (T2), is CI-checked across the docs (ARCH §6.2, §12.2) |
| 4 | Customer sends under the trigger | Every send is an outbox row; a third authorization path, `comms_plan_id`, accepts customer-comms kinds only, checks plan approval + `plan_hash` + template; plan approval is a proposal/approval pair (RBAC, no-self-approval); auto-sent field values must come from **human-set record state** or the schedule, never agent inference (ARCH §6.3) |
| 5 | Fix dedup | Index on `status <> 'resolved'` (covers `human_owned`, `reopened`); human-filed tickets fingerprint by identity; `created` derived from `duplicate_count = 0` (not `changes()`); the double count is removed; material change = severity escalation only; no time window (the lifecycle is the window); `case_groups` storm grouping specified (ARCH §4.1, §4.3) |
| 6 | Fix RCA | Indentation early-return fixed; entailment acts on claims after `reduce()`, never on the outcome; binding is `$service.*` in args and the CEL check; **CONFIRMED renamed SUPPORTED**; RCA outcomes never drive customer comms in v0.x (`comms_if_supported` internal only) (ARCH §5, §5.1, §5.4) |
| 7 | Decide `create_page` and the free-text calibration | `create_page` is **out of v0.x**, and an external pager is a `doctor`-checked prerequisite (ARCH §14). `calibrated_score` fails closed (no entry → 0); free text routes P3/P4 on the LLM label, while an LLM-only P1/P2 goes to a human with the suggestion (ARCH §4.2) |
| 8 | Make the gates achievable | Wilson math stated; holdout sized at 150–250 (≥ 60 P1/P2, ≥ 120 claim pairs); the bound is computed over cases; all thresholds are **(prov.)**, set from v0.0 data via ADR; frozen eval snapshot in the cache key; model-deprecation runbook; shadow promotion uses the lower bound (ARCH §9, §11; CAP §4.7) |
| 9 | Reconcile CHANGELOG ↔ spec and cross-doc contradictions | Inaccurate round-1/2 rows corrected in place (*R3-corrected*); CAP design rule 5 updated; C7 parameter question is T2; the duplicate failure table is removed; the diagrams no longer show redaction as a stage; the PITCH overclaims are rewritten; the ROADMAP v1.0 exit is the value story; ROADMAP §2 is now a pointer table |
| 10 | Freeze and build | ARCH marked **FROZEN**; post-freeze changes via ADR on code PRs; every ARCH §19 test committed as **failing in week 1**; doc-lint tests (`test_docs_action_kinds_canonical`, `test_changelog_anchors_resolve`) (ARCH header, §19; ROADMAP Phase 0) |

### Other round-3 findings — accepted

| Finding | Fix |
|---|---|
| Postgres twin of the SQLite-dialect triggers | Invariant suite runs on **both engines**; PL/pgSQL twins live in migrations (ARCH §2, §15) |
| INV-06 canaries are tautological | Canaries prove the path is wired in; **detector recall** on a held-out realistic-secret corpus is a separate quality metric (ARCH §6.1, §11) |
| v0.0 needs identity that arrives in v0.1 | OIDC login + `users` + `trg_no_self_approval` moved into v0.0; default T1 grants are approved by the customer admin at **first-run setup**, not vendor-seeded (ARCH §6.4; ROADMAP Phase 1) |
| Assignment automation looks like takeover | Human actor = not integration user, not `system`, not on the confirmed automation-actor list (`doctor --discover-automation`) (ARCH §8.2) |
| Poll-cursor loss and visibility modes | Overlap window + `(sys_id, sys_mod_count)` dedup; per-change `sys_audit`/`sys_journal_field` history; separate `em_alert` cursor; poll interval in the SLO; precise loss model (ARCH §8.1) |
| Assisted delta needs a control group; promotion is commercial | Treatment-vs-control over the same period; assisted promotion tracked as a partner dependency (ARCH §8.4; ROADMAP §5) |
| Entailment latency vs Ollama | Per-artifact budget (prov.); slow providers produce more `speculation`, never a pass (ARCH §5.4) |
| Stale pinned-model examples | Examples replaced by the "dated snapshot ID" rule plus a deprecation runbook (ARCH §9) |

### Pushback

None this round. One judgment call is recorded: v3 offered "rule-authorized T1 `create_page`" *or* "out of v0.x". We chose **out of v0.x**. A T1 page would need a grant, grants for paging would reopen the pager-DoS question, and the simpler product (external pager required, agent enriches) has no 3 a.m. dependency on us.

---

## Revision log (what was wrong, and where it was corrected)

These items were genuinely wrong or buggy in earlier drafts. They are logged here so the normative docs don't carry the correction prose.

1. **"No exceptions" approval wording** — contradicted the T1/allowlist model it sat next to. → Corrected promise, stated once (ROADMAP L7, CAP §2 L39).
2. **ServiceNow fingerprint = ticket `number`** — conflated identity with dedup; two distinct tickets could collapse, or one outage could split. → Normalized signature, INV-02 (ROADMAP L89, ARCH §4.1).
3. **`count` column** — referenced in the dedup design but absent from the schema. → `duplicate_count` (ROADMAP L146, ARCH §15).
4. **"Exactly-once / structurally impossible double-sends"** — not deliverable. → At-least-once + effectively-once via reconciliation; 0 double-sends is a *measured* soak (CAP §4.1, INV-04).
5. **`SELECT … FOR UPDATE`** — does not exist in SQLite. → `BEGIN IMMEDIATE` + status CAS (ROADMAP §2.4).
6. **RCA loop iterated LLM-chosen hypotheses; NL `expected_evidence`** — non-replayable, non-verifiable. → Code-owned step plan + CEL predicates (ARCH §5.1).
7. **INV-01 as a `CHECK` constraint** — cannot see status/hash/grant validity. → FK + `PRAGMA foreign_keys=ON` + `BEFORE INSERT` trigger + 4 tests (ARCH §6.2, INV-01).
8. **Redaction wrapped only `classify`** — hypotheses/conclude/entailment/draft could leak. → Redaction inside `LLMClient.structured()`, the only provider path (ARCH §6.1, INV-06).
9. **"MTTR delta in shadow"** — zero by construction, not a value story. → Counterfactual vs assisted delta, two mode-recorded outputs (ARCH §8.4).
10. **`sys_comment` as one action; `enrich_page`/`create_page` conflated** — the tiering was wrong. → `work_note` (T1, caller-invisible) / `customer_comment` (T2, visible, gated); `enrich_page` (T1 attach-only) / `create_page` (T2 rule-only, never LLM-decidable) (CAP §2, ARCH §14).
11. **Free-text grant allowlist** (`approved_by: string`, `expires: 90d`) — unenforceable, unanchored. → DB-only grants with `granted_by`/`granted_at_ts`/`valid_to_ts` (ARCH §6.4).
12. **DLP labeled C18** — C18 is LLM-bound redaction (INV-06); outbound comms DLP is C4. → Relabeled (CAP C18/C4 rows).
13. **"Cases generated from the source incident"** — train-on-test. → Holdout disjoint in time AND signature, never generated from the source incident (CAP C13/C14 rows, ROADMAP phase 10).
14. **Week re-cut** — v0.1 previously carried two-person rule + DLP + comms matrix in 10 weeks. → v0.0 wk 1–4 / v0.1 wk 5–12 / v0.2 wk 13–18 / v0.3 wk 19–24 (CAP release map, ROADMAP §3).
15. **`retriage` event kind** — not canonical and not needed. → Material change appends a `decision` event (ROADMAP §2.4, ARCH §15).
16. **C21 (two-person rule) in the v0.0 scope list**: a v0.2 feature. → v0.0 scope = trust chain + OIDC identity + no-self-approval + C16 shadow + C18 redaction (CAP release map; round 3 moved identity into v0.0).
17. **Litestream timing** — "v0.1 PITR" vs the v0.2 cut list. → Reconciled: ops item v0.1; *named + tested PITR capability* v0.2 (ROADMAP §2.7, phase 10b).
18. **C20 ambiguity** — storm grouping vs spend breaker read as two features. → C20 = alert-storm handling *including* the LLM-spend circuit breaker (CAP C20 row, ARCH §11).
19. **ARCH L476 Litestream wording** — "v0.1" conflicted with the v0.2 cut. → "ops item v0.1; named+tested PITR v0.2".
20. **ARCH spend-breaker line listed the stale `escalate` action kind** (no longer canonical). → `safe_default` classification + `enrich_page` T1 attach-only + no new proposals (ARCH §11).
21. **Dangling `§11.2` self-references in ARCH §11** (§11 has no numbered subsections). → Point at the §11 SLO bullet / drop the ref (ARCH §11).
22. **`§13` pointer for the design-partner dependency** (§13 = non-goals, doesn't cover it). → Points to CAP "Hard dependency" + ROADMAP risk section (ARCH §11).

---

## Deferred (accepted as honest non-goals for v0.x)

- **Ticket attachments / screenshots** (round 1 §8.12) — deferred; not claimed in any doc.
- **Multilingual comms** (round 1 §8.13) — deferred; not claimed in any doc.
- **Zendesk adapter** — second inbound adapter, v0.3 (ROADMAP phase 12).
- **Slack/Teams approval buttons** — v0.2 comms matrix (ROADMAP L132).
- **Multi-tenant operation** — v0.3; schema tenant-ready from v0.0.
- **Helm/K8s** — v0.3.
- **`cron` re-invoke** — deferred until a constrained runner is specified (code-execution capability; ARCH §7).
- **Cross-encoder reranker** — v0.2 (ROADMAP Phase 9).
- **Page creation (`create_page`)** — not in v0.x; external paging required (ARCH §14). Post-v1.0 ADR if ever.
- **Feature-flag and metrics/APM evidence sources** — v0.2 (ARCH §7).
- **LangGraph/CrewAI, Kafka, K8s, managed vector DB, LangSmith-as-dependency** — non-goals (ARCH §16).

## Open items (need a first-customer answer)

Tracked in CAPABILITIES §6 — log-source shape, Airflow API exposure, GitHub Actions runner type, SSO choice, comms channel for the reference deployment, **design-partner data access (blocking)**, RCA evidence sources, SLA calendar.
