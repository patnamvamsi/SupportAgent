# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Every role is primary; the UI serves them equally (maintainer, 2026-11-28):

- **On-call engineer / analyst:** opens a case when an alert or ticket arrives, reads what the agent decided and why, approves or rejects proposals, records an alert case's outcome.
- **Approver / team lead:** approves proposals, comms plans and grants; watches stats, readiness and promotion.
- **Admin onboarding a system:** reviews the system graph, catalog, detectors, questions and access requests the instance raises about the system it supports.

The bar: **a newcomer who has never seen the supported application should be able to open the UI, understand the system, and triage it** without reading the docs first.

## Product Purpose

SupportAgent is a self-hosted application support analyst. It triages alerts and ServiceNow incidents, runs bounded read-only root-cause checks, drafts communications and proposes actions. Anything that changes state or reaches a customer goes through an approval the database enforces. It starts in shadow mode and only acts once promoted.

Success for the UI: a person understands the supported system and the agent's reasoning quickly enough to decide, at 3 a.m., without discovering things by hand.

## Positioning

"Not another AI copilot chatbox: a deterministic support pipeline with LLM slots" (docs/PITCH.md). The state machine is the product. What the UI must show and a chatbox can't: every decision is replayable from its trace, every claim cites evidence, and every action traces to one human authorization.

## Operating Context

- Self-hosted, often on a laptop or a single host; ports bind to 127.0.0.1. Must work offline: no CDN, no external fonts or scripts.
- Signed in through a bundled Keycloak (OIDC); RBAC roles viewer, analyst, approver, admin.
- Data the UI presents: the learned system graph (components, kinds, relations, proposed vs confirmed facts, summaries with citations), cases (classification, severity, decision with reason, RCA with evidence steps, claims, comms plans, proposals, outbox sends, event timeline and trace), transaction traces through reviewed flows, stats/promotion, onboarding (graph, catalog, detectors, questions, requests, readiness).
- Signals come from the supported system's Prometheus and Loki; logs are reached through those.

## Capabilities and Constraints

- Server: FastAPI + Jinja templates under `src/supportagent/entrypoints/api/`; static files served by the app at `/static/`.
- CSP is strict and stays: `script-src 'self'; style-src 'self'`; no inline script or style; forms post to self with a CSRF token.
- Interactivity may go as far as needed, including a client-side app, provided every dependency is free and open source and vendored (served from `/static`, never a CDN). No paid frameworks, libraries or services.
- Wanted (maintainer, 2026-11-28):
  - a super-interactive knowledge graph with a 3D presentation of key elements; drill into a node to reveal more nodes and details;
  - showcase how the agent makes decisions (the triage branches, RCA evidence, approvals);
  - easy log drill-down and analysis.
- The agent never pages, never sends unapproved customer comms, and RCA never drives customer comms: the UI must not imply otherwise.
- Terminology: case, triage, classification/label, severity P1–P4, safe default, shadow mode, proposal, grant, comms plan, RCA verdicts SUPPORTED / CONSISTENT_UNCONFIRMED / INCONCLUSIVE, readiness, calibration.

## Brand Commitments

- Name: SupportAgent. No logo, palette or typeface is fixed.
- Light and dark themes both stay, following the OS setting.

## Evidence on Hand

- Product and positioning prose: `README.md`, `docs/PITCH.md`, `docs/spec/`.
- A real proving-ground deployment exists, but its name and data never appear in product code or UI (ADR-0022). Demo and fixture data only.
- No testimonials, customer logos or benchmarks to show; do not fabricate them.

## Product Principles

1. **Show the reasoning, not just the result.** Every decision links to its reason, evidence and trace.
2. **Honest states.** Proposed vs confirmed, held vs did-not-run, speculation vs supported claim are always visually distinct.
3. **Explorable by a newcomer.** Start from the system's shape, drill into detail on demand; no prior knowledge needed.
4. **The human decides.** Approval and outcome controls are clear, deliberate and never ambiguous.
5. **Self-contained.** Works offline under the strict CSP.
