# 0021. Transaction tracing: where is this business object, and is that where it should be?

- **Status:** accepted (2026-11-08; roadmap item 1.5 in `docs/next-steps.md`, after the first real system)
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

## Decision

### 1. A separate read-only capability, alongside triage

A new investigation answers one question: *where is business object X in flow F, and does that match what F says should happen?* A support engineer asks it from the case page or the CLI (`supportagent trace <flow> <id>`), optionally against a ticket.

It doesn't classify, route, claim or propose. Its output is an answer with cited evidence on the case timeline. Any action still goes through the existing kinds and the approval gate.

### 2. Expected behaviour is a reviewed flow file, not the specs

A **flow** declares one business process as data, in the system's onboarding workspace:
- its stages, in order, with the component that performs each one (a graph node such as `service:enrichment`);
- for each stage, how to tell it happened for one ID: a declared capability and its parameters, for example a log query, a row lookup or a topic offset search, with the ID as the only free parameter;
- the time each stage should take, and which stages are terminal;
- optionally, the condition under which a stage is skipped or the object is rejected on purpose.

Onboarding first proposes **candidate flows** from the ticket history: the business objects tickets ask about most ("order 123 not shipped", "trade XYZ123 not settled"), with how often, which components the resolutions named, and whether the IDs in those tickets show up in the system's logs. The maintainer picks one. Onboarding then drafts that flow from the docs, code and graph, the way `onboard catalog` drafts the catalog. Each draft cites the documents it came from and is marked unreviewed. **Only a flow a person has reviewed is used.** A stale spec then produces a draft someone corrects, not a wrong answer.

The graph gains `Process` and `Stage` nodes, with a `performed_by` edge to the components. It stays the existing graph: no separate graph database.

### 3. Correlation is verified, never assumed

Onboarding checks each stage's lookup against a handful of sample IDs a person supplies, taken from recently completed objects. A stage whose lookup can't find a completed object is reported as a **correlation gap**: a *Complete the configuration* need naming the stage and what was tried.

At question time, a stage with a correlation gap reads "unknown", never "missing". The agent may derive a secondary key from an earlier stage (an order ID becoming a message key) only where the flow declares that mapping.

### 4. Logs and messages first; business records only when they aren't enough (maintainer's decision)

Each stage's lookup is a log query or a message search where it can be. A flow may declare a business-record lookup for a stage only when onboarding showed that logs and messages can't establish that stage: the sample IDs aren't found there, or the stage's outcome (a status column, a rejection reason) is recorded only in the database. Onboarding reports this per stage as a need ("stage *booking* can only be established from `trades.status`"), so a person sees why a record read is asked for before granting one.

A credential for business data is bound like any other (ADR-0019), and must also meet these rules:
- **Declared queries only.** Every lookup is a flow stage's declared query, parameterised by one ID. There is no free-form query, and nothing that lists or scans records.
- **Field allow-list.** Each stage declares the fields the model may see. Everything else is dropped before the model sees the result, and the existing secret redaction still applies.
- **Audit.** Every lookup is an event on the case timeline: who asked, which flow, which ID, which stages were read.
- **Opt-in per flow.** A flow that reads business records stays off until an admin enables it, as with grants.

The capability is `records.query`, over a business-data connection kept apart from the `pg_monitor` one (`tools.records`). Its SQL is a reviewed literal from the flow or pack, never the model's: one `SELECT`, whose only inputs are `:id` and `:key`, sent as bound parameters. The session is read-only with a statement timeout, and at most 50 rows come back. The database must be one the stage's catalog service lists in `databases:`.

**Investigations use the same credential (maintainer's decision, 2026-11-08).** The read-only grant isn't only for tracing: a pack's probe may use `records.query` too, to debug a data anomaly (gaps in market data, for example) or anything else the records show. The rules that still hold are reviewed SQL only (never the model's), the read-only role and the audit of every call. A probe's query can aggregate over a window instead of reading one ID's rows. Its result reaches the model only as cited `entail` evidence, which isn't pseudonymised. So a probe selects aggregates and non-personal columns, and that is part of reviewing it. The role is granted no credentials or session tables.

### 4a. PII is detected and pseudonymised before the model sees anything (maintainer's decision)

Logs and messages carry personal data as often as database rows do, so this applies to **every** stage result, not only record reads:
- **Detection:** [Microsoft Presidio](https://microsoft.github.io/presidio/) (MIT, runs locally, no data leaves the deployment), behind the existing secret-detector port. It adds PII classes (person, address, national ID, IBAN, account number and so on) to the closed detector set. Each deployment chooses which classes are on and may add recognisers for its own identifiers. The current regex detectors keep running alongside it.
- **Pseudonymised, consistently:** a value becomes a stable placeholder (`<PERSON_1>`) for the whole question, so the model can still tell that two stages saw the same customer. The map stays in memory, as with secrets today (INV-06), and is never stored, logged or sent.
- **The traced ID is allowed through:** the object's own ID is the one value the model must see. Values the flow marks as keys pass through too, unless they are themselves PII, in which case the model sees a placeholder and the lookup uses the real value.
- **The person who asked sees the real values (maintainer's decision, 2026-11-08):** on the trace page, the answer and its facts show what the logs say (the placeholders are mapped back in memory, from the same map, at render time). The model only ever saw placeholders, and nothing that is stored, logged or sent changes. The page is behind the `trace` permission and each trace is audited as its user.
- **The field allow-list runs first:** fields not on it never reach detection or the model.
- **Fails closed:** if the analyser isn't installed or errors, no stage result reaches the model. The answer says tracing is unavailable and why. A flow can't be enabled while `doctor` reports the analyser missing.
- **Recorded:** per-class counts of what was pseudonymised go on the case timeline, never the values.
- **Packaging:** Presidio and its language model are an optional install (`supportagent[pii]`), so deployments that never trace don't carry them. The image used for tracing includes them.
- **Measured:** the flow lab includes records and log lines seeded with PII. A gate requires that none of it appears in any recorded model request.

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

**The calibration floor doesn't apply (maintainer's decision).** ADR-0020 holds a class back until about 16 live incidents per label. Tracing doesn't route or propose, so a flow is usable once its lab passes. Tracing is useful from the first day on a new system, while routing builds its proof in shadow. Resolved live questions still feed the flow's lab, as resolved cases feed calibration.

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

## Maintainer's decisions (2026-11-08)

1. **Direction:** accepted.
2. **Calibration:** a flow is usable once its own lab passes (§6).
3. **First flow:** chosen on the real system from the candidates onboarding proposes (§2).
4. **Data:** logs and messages first, business records only where they can't establish a stage (§4), with PII detected and pseudonymised by Presidio before the model sees anything (§4a).
