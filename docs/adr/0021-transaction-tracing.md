# 0021. Transaction tracing: where is this business object, and is that where it should be?

- **Status:** proposed (2026-11-08; roadmap item 1.5 in `docs/next-steps.md`, after the first real system)
- **Date:** 2026-11-08
- **Code PR:** none yet
- **Builds on:** ADR-0011 §4 (the graph guides, tools prove), ADR-0015 (onboarding), ADR-0016 (plugins are data), ADR-0018 (live evidence), ADR-0019 (bound read-only credentials), ADR-0020 (readiness and the calibration floor)

## Context

SupportAgent answers "what is wrong with this system?". A ticket or alert comes in, triage gives it an incident class, and RCA tests that class's causes against tools. The unit of work is an incident class, and "wrong" means that tool results support a known cause.

Many support questions aren't like that. "Trade XYZ123 was submitted but never reached settlement" is about one business object. Every system can be healthy while that trade is stuck, so no alert fires and no incident class matches. To answer it, someone must know the stages the trade should pass through, find this trade at each stage, and say where it stopped and why. The maintainer raised this from an outside write-up ("business-to-production intelligence"). Its central idea is comparing expected behaviour with actual behaviour, and that is a real gap.

Several problems the write-up glosses over shape this decision:
- **Specs aren't truth.** Requirements and specifications go stale and contradict the code. Many systems have none. An expected lifecycle built from them unchecked gives confident, wrong answers.
- **Business data is a different risk.** Reading trade records means reading customer data. ADR-0019 proves a credential can't write. It says nothing about what the credential can read.
- **Correlation is assumed.** Following one ID across services only works where that ID is actually logged, stored or carried in messages.
- **Being wrong needs an answer.** The agent needs a safe default when it can't tell, just as triage and RCA have one.

The write-up also suggests LangGraph, Neo4j and several cooperating agents. None of those solves the problems above, and the existing graph, tool and lab machinery already covers what they would do.

## Decision (proposed)

### 1. A separate read-only capability, alongside triage

A new investigation answers one question: *where is business object X in flow F, and does that match what F says should happen?* A support engineer asks it from the case page or the CLI (`supportagent trace <flow> <id>`), optionally against a ticket.

It doesn't classify, route, claim or propose. Its output is an answer with cited evidence on the case timeline. Any action still goes through the existing kinds and the approval gate.

### 2. Expected behaviour is a reviewed flow file, not the specs

A **flow** declares one business process as data, in the system's onboarding workspace:
- its stages, in order, with the component that performs each one (a graph node such as `service:enrichment`);
- for each stage, how to tell it happened for one ID: a declared capability and its parameters, for example a log query, a row lookup or a topic offset search, with the ID as the only free parameter;
- the time each stage should take, and which stages are terminal;
- optionally, the condition under which a stage is skipped or the object is rejected on purpose.

Onboarding drafts flows from the docs, code and graph, the way `onboard catalog` drafts the catalog. Each draft cites the documents it came from and is marked unreviewed. **Only a flow a person has reviewed is used.** A stale spec then produces a draft someone corrects, not a wrong answer.

The graph gains `Process` and `Stage` nodes, with a `performed_by` edge to the components. It stays the existing graph: no separate graph database.

### 3. Correlation is verified, never assumed

Onboarding checks each stage's lookup against a handful of sample IDs a person supplies, taken from recently completed objects. A stage whose lookup can't find a completed object is reported as a **correlation gap**: a *Complete the configuration* need naming the stage and what was tried.

At question time, a stage with a correlation gap reads "unknown", never "missing". The agent may derive a secondary key from an earlier stage (an order ID becoming a message key) only where the flow declares that mapping.

### 4. Business-record reads are their own access class

A credential for business data is bound like any other (ADR-0019), and must also meet these rules:
- **Declared queries only.** Every lookup is a flow stage's declared query, parameterised by one ID. There is no free-form query, and nothing that lists or scans records.
- **Field allow-list.** Each stage declares the fields the model may see. Everything else is dropped before the model sees the result, and the existing secret redaction still applies.
- **Audit.** Every lookup is an event on the case timeline: who asked, which flow, which ID, which stages were read.
- **Opt-in per flow.** A flow that reads business records stays off until an admin enables it, as with grants.

### 5. The answer: a timeline, the first deviation, then the cause

1. Look up each stage for the ID and build the actual timeline from tool results only.
2. Compare it with the flow. The **first deviation** is the earliest stage that is missing, late, or reached a state the flow doesn't allow.
3. If the deviation names a component, hand off to RCA, scoped to that component and the deviation's time window, using the packs that already exist. A rejected message, for example, leads to that consumer's checks.
4. Write the answer at two levels: one for the engineer (stage, evidence, cause) and a short summary in business terms ("booked, but waiting for enrichment because counterparty reference data is missing"). Every sentence cites a stage result or RCA claim, and the existing `entail` faithfulness check applies.
5. If a stage is unknown, or the evidence doesn't decide, the answer says so and names what is missing. "Couldn't tell, because stage 3 can't be looked up" is the safe default, and a guessed stage never is.

### 6. Its own accuracy bar

A lab per flow: 20–30 labelled questions with recorded tool results, covering completed objects, objects stuck at each stage, deliberate rejections and objects with missing correlation. Proposed gates:
- zero claims of the wrong stopping stage (like zero false causes);
- the first deviation found in at least 90% of the stuck cases;
- every "couldn't tell" answer names the missing stage.

**Open: whether the calibration floor applies.** ADR-0020 holds a class back until about 16 live incidents per label, so a new system takes weeks to earn routing. Tracing doesn't route or propose, so this ADR proposes that a flow is usable once its lab passes, without waiting for live calibration. That would make tracing useful from the first day on a new system, while routing builds its proof in shadow. The maintainer decides.

### 7. Not doing

- Several cooperating agents, LangGraph or Neo4j. One investigation over the existing graph, tools and labs is enough, and new frameworks add risk without solving the hard parts.
- Answers sent directly to business users. Engineers come first, until the lab and some live use show the answers hold.
- Actions taken from a trace, beyond what existing kinds and grants allow.
- Flows inferred with no review.

## Consequences

- A class of question the product can't answer today becomes answerable, including stuck objects on healthy systems, which raise no alert.
- Onboarding gains a step (draft flows, verify correlation) and a new kind of need (correlation gaps). A system without correlation IDs learns that early, which is useful in itself.
- Reading business records widens what a credential is trusted with. The declared queries, field allow-list and audit keep that bounded, but each flow that reads records is an admin decision.
- The model sees business fields it never saw before: more tokens per question, and a new reason for local models in regulated deployments.
- It depends on the real system (roadmap 1.4). Flows should be fitted to a real system's processes and logs, not a synthetic one. The reference systems would each get one flow as a regression fixture, as they do today for classes.

## Questions for the maintainer

1. Accept the direction, or park it?
2. Calibration (§6): may a flow be used once its lab passes, before live calibration?
3. Which flow on the real system to start with. One flow first, end to end.
4. Whether business-record reads are acceptable on that system at all, or tracing starts from logs and messages only.
