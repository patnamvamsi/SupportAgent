# SupportAgent — The Pitch

> Positioning and sales prose live here. The normative docs — [ARCHITECTURE.md](spec/ARCHITECTURE.md), [CAPABILITIES.md](spec/CAPABILITIES.md), [ROADMAP.md](spec/ROADMAP.md) — state what the product does and how; this file states why it wins.

## One line

**SupportAgent is not another "AI copilot chatbox." It is a deterministic support pipeline with LLM slots.** The state machine *is* the product; the LLM is a typed, budgeted component inside it.

## The 3 a.m. story

It's 3 a.m. and the production database pool is exhausted. In the world we're replacing, the on-call engineer gets woken by a page, opens a laptop, reads the ticket, greps the logs, checks whether a deploy went out an hour ago, drafts the customer update *from memory of what they've seen before* — and every one of those steps is a place for a tired human to be wrong.

In our world, the pipeline has already done the work:

1. **Ingest + redact.** The alert is a typed `AlertEvent`. Every secret or PII token our detectors flag is replaced before any LLM call (INV-06, enforced *inside* the LLM client, so no call site can skip it), and detector recall is measured on a held-out corpus rather than assumed.
2. **Dedup** — the normalized signature matches the open case from 20 minutes ago; this alert increments `duplicate_count`, it does not spawn a second case (INV-02, enforced by the schema).
3. **Classify.** The deterministic pre-classifier and the LLM agree on `database/saturation`, P1. If they disagree, or if only the LLM thinks it's a P1, the case goes to a human with the reason and the suggestion attached. It never guesses (INV-05).
4. **RCA-lite.** The code-owned runbook steps run in order: a failed run in this service's repos? a recent change? pool-exhaustion errors in its logs? Each hypothesis ends SUPPORTED, CONSISTENT_UNCONFIRMED or INCONCLUSIVE, which describes the evidence and claims nothing about cause. Every claim links an evidence ID, anything a citation doesn't support is tagged `speculation`, and **nothing about cause goes to the customer unless a human writes it**.
5. **Propose.** An internal work note (auto, T1, under a grant the customer's admin approved at setup), a customer status email (**gated**, T2), and context attached to the page that the customer's *own* monitoring already opened (`enrich_page`). The agent never creates pages, so the 3 a.m. path never depends on it.
6. **Approve.** The on-call engineer, now awake with a laptop, opens the admin UI: one proposal, one diff, one approve. The customer email goes out. The trace ID is on the proposal, the proposal is on the approval, the approval is on the outbox row, and the approval can be used exactly once. **The whole decision is replayable.**

The human's job at 3 a.m. is *deciding*, not *discovering*. That is the product.

## What it is

A deployable "application support analyst." A team deploys **one container**, writes a **`supportagent.yaml`**, sets env secrets, and points it at their ticketing and alerting systems (ServiceNow first; generic webhook; Zendesk by v0.3). It triages alerts, manages the ticket lifecycle, drafts communications, and proposes job re-runs — with **SQLite by default** (Postgres behind a flag) and a `supportagent doctor` that verifies the whole trust chain before anything ships. Framework-agnostic core: the inbound adapter, the LLM provider, and the outbound tools are all replaceable; the loop is not.

## The differentiators

Against "just add an AI assistant to ServiceNow," we win on four things the chatbox can't offer:

1. **Replayability.** Every tool call, token, and decision is an append-only `events` row with a `trace_id` (INV-03). Any triage decision is replayable through the full pipeline — same code path as production. An audit report can be generated from *any* trace.
2. **Auditability, at the DB layer.** The promise, *every state-changing or customer-facing execution traces to exactly one valid authorization: a single-use human approval, a customer-approved grant, or a human-approved comms plan*, is enforced by foreign keys, INSERT and UPDATE triggers (authorization is immutable once written), and literal kind lists that keep customer-facing actions off the policy path. Not a prompt. Not a middleware convention. The database, on both supported engines.
3. **Honest guarantees.** We don't claim "structurally impossible double-sends" — we claim **at-least-once delivery + effectively-once effect**, and then prove it: a 1,000-alert storm soak with **0 duplicate cases and 0 double-sends, measured** (ARCH §19). We don't claim "no PII ever reaches a provider". We claim the redaction path is wired into every LLM call (the canary suite proves it), and we publish detector recall on a held-out corpus of realistic secrets. When the LLM times out, the case ends in a safe-default state with zero unapproved side effects (INV-05, proven by fault injection, not by hope).
4. **Learning with a guardrail.** The agent can *propose* changes to itself — a distilled runbook, a prompt patch — but only the eval gate decides, and the gate includes a holdout **disjoint from training in time AND signature**. Cases may be added; never loosened. The non-learnable surface (the gate, the tier model, the eval structure, the routing table, the taxonomy) is sacred.

## The signature feature: the approval promise

*Propose → human approves → execute* applies to T2 and above. T1-internal actions (internal notes, watchlist, enriching an existing page) are policy-allowed by default — which is why "internal status auto-posted" works in the demo without breaking the promise. Either way:

- **every execution traces to a human approval or a customer-owned `policy_grants` row** — the YAML file is a *request*; the database is the source of truth;
- **no config turns the gate off**;
- **customer-facing actions are never grant-authorized**, and page creation doesn't exist in v0.x. The database trigger makes both structural;
- **the agent never self-approves, never widens a grant, never approves via a ticket comment** (a customer commenting on their own ticket is a privilege-escalation path — we close it by making it invalid).

That is not a feature list. That is the reason a bank, a carrier, or a SaaS company can put this in front of customers at 3 a.m.

## The value story (honest)

We report **two separate, mode-recorded numbers** and never conflate them (C22):

- **Counterfactual baseline (shadow mode, v0.1):** alert→first-proposal time, proposal correctness, and triage agreement with human triage, gated on a *lower confidence bound*, not a headline average. In shadow the agent takes no action, so any MTTA/MTTR delta is **zero by construction**, and we say so out loud.
- **Assisted delta (live, v0.2 onward):** MTTA, time-to-first-customer-update and MTTR on services the agent assists, **compared against a control set of services that stay human-only over the same period**, with confidence intervals. That is a real treatment-vs-control comparison, not a before/after chart.

The v1.0 exit story is that number, plus INV-01..06 green in CI, plus a full audit trail. That is the closer: **we show the delta, not the adjectives.**

## Regressions get caught in CI, and we're precise about which

A prompt regression is the classic "AI product" failure mode: the model drifts, the behavior changes, and nobody notices until a customer does. Here, two kinds of regression are handled two ways:

> **Invariant regressions never ship.** A bad routing row, a broken schema, or a code path that would execute without approval turns the invariant suite red (single-sample, must-pass, zero-tolerance, both database engines). Merge blocked.
>
> **Quality regressions beyond the gate are blocked.** A bad prompt change runs against the real LLM on a frozen eval snapshot. If the per-severity lower confidence bound drops below the gate, the per-case diff shows exactly which alerts regressed, and the merge is blocked. A small regression inside the statistical band can still ship. We say so, and we track the trend. The same five pillars that review the agent's daily work review its own evolution: self-changes (runbooks, prompt patches) pass the **strictest** gate, deploy versioned, and roll back with one command.

## The data story (and why it's clean)

The eval corpus is where "AI support" claims usually die. Ours is sourced, and the licensing is handled:

- **Primary:** the design partner's historical incident history — tickets, alerts, logs, resolutions, ≥6 months. The only corpus with real *inputs*, not just real labels. (A hard dependency, tracked as the top project risk from day 0.)
- **GitHub Status:** real public incidents with full published timelines — e.g. *"[Retroactive] Actions workflow run logs delayed"* (Oct 13, 2026) — where the vendor's own status updates are the ground truth for what a customer should have been told.
- **Atlassian status pages:** realistic "customer-facing update" expectations.
- **Google SRE Workbook:** the **68% change-induced** statistic from Appendix C, **cited as a fact only**. The text is CC BY-NC-ND, so we never redistribute or adapt it and build no eval cases from it. The NC is a real risk we plan around, not a footnote.

≥50% of the v0.1 corpus is source-attributed and licensing-clean. That is an exit criterion, not an aspiration.

## The closer

> "Your on-call engineer is already stretched. The last thing you should be adding to their 3 a.m. is a chatbot that needs babysitting. SupportAgent runs the support pipeline — triage, dedup, RCA, comms, re-run proposals — deterministically, with the LLM in a typed slot where it's good and gated where it must be. Every action is traceable to exactly one authorization your auditors can read. Invariant regressions can't ship, and quality regressions beyond the gate are blocked in CI. And when it learns from your cases, into case memory and reviewed runbooks, the eval gate decides, the holdout is disjoint in time and signature, and one command rolls it back. That's not a copilot. That's a support analyst that answers for itself."

---

*Every claim in this file maps to a normative statement in ARCHITECTURE.md, CAPABILITIES.md, or ROADMAP.md. Where this file and those disagree, they win — and the disagreement belongs in [CHANGELOG.md](CHANGELOG.md).*
