# 0020. Shadow replay: readiness also asks whether the pipeline would route it

- **Status:** accepted (2026-11-05; roadmap item 1.3 in `docs/next-steps.md`)
- **Date:** 2026-11-05
- **Code PR:** WP-O6c
- **Builds on:** ADR-0002 (calibration), ADR-0015 §5 (readiness from history), ADR-0017 (fitted calibration), ADR-0018 (live evidence), ADR-0019 (bound credentials)

## Context

Readiness replays each past incident through the real classifier and RCA, with tools answering from evidence. That proves the agent can find a class's cause. It doesn't run dedup, storm grouping, the calibration floor, the severity cap or routing, so it can't say whether the pipeline would ever let the agent act.

Two kinds of class also stayed out of reach. Causes that are visible only live (Oracle locks and sessions, since there is no Diagnostics Pack licence) have nothing to replay, so they could never be proven. And nothing connected live cases back to readiness.

## Decision

### 1. The history goes through the shadow pipeline too

After the lab replays, each mapped past incident goes through `ShadowRunner` on a throwaway database, in the order it was opened and with the clock at that moment. It runs with the calibration this run fitted and the routing policy of the lab's verdicts. It needs no evidence, so every incident of a class counts, including those the lab couldn't replay.

A class the lab made ready stays ready only when shadow routes `min_routed` (75%) of its incidents with its label, each on its own case.

### 2. Two verdicts

- `lab_status`: the lab's proof. The lab's regression baseline (`readiness_baseline.json`) guards this one.
- `status`: whether the pipeline would route it too. This is what the bar and routing read.

Without the split, one calibration change would flip every baseline entry and the baseline would stop catching lost proof.

### 3. The calibration floor stays, and live incidents reach it (maintainer's decision)

The first run showed that **every class in all three reference systems fails on calibration, so they all measure 0% ready**. ADR-0002 needs at least 10 observations and a Wilson lower bound of `min_score` (0.80), which takes 16 correct in a row. The reference histories hold 3 to 7 incidents per class.

The options were a lower floor for onboarding, pooling calibration by domain, reporting shadow as advisory only, or keeping the floor and learning live. The maintainer chose the last: a safe default is the right behaviour until a label has earned routing, and an advisory number would overstate readiness.

So every resolved live case becomes a calibration observation: what triage predicted against the label onboarding later mapped its ticket to. A class held back this way says what it waits for: `learning live: database/latency (with no pre-classifier label) needs 11 more correctly classified incident(s) (5/5 so far)`. The status page lists it as a *Run in shadow* need.

Expect a system to stay in shadow for as long as it takes to see about 16 incidents per label. A label that rarely fires may take a long time.

### 4. Classes are proven live

In shadow mode the `rca.run` job also runs on a case held back with a label (`class-not-ready`, `low-calibrated-score`, `unconfirmed-high-severity`), using the bound read-only credentials while the incident is happening. Shadow has no gate, so nothing is proposed from it. The job refuses this setting when it has a gate.

Once the ticket resolves and onboarding maps it to a class, an incident with no replayable evidence counts with that RCA's result. It is marked `proven_live`. Only resolved cases count, because the class is known from the resolution, never from the agent's guess. An incident counts once, for calibration as well as for proof.

### 5. Catalog gaps are configuration, not class failures

Triage refuses a service that isn't in the catalog or has no paging integration, and onboarding doesn't write the catalog. Counting that against each class would hide what the replay measures, so shadow gives those services a placeholder integration and reports each one as a *catalog gap* and a *Complete the configuration* need.

## Consequences

- A system's ready share now falls to 0% until live evidence arrives. This is the true state: fitted calibration already held every label back after onboarding (ADR-0017). That smoke-grade calibration can't route was known since v0.0 (an open ADR-0003 question); what's new is that readiness measures it per class and says what each label waits for.
- `onboard run` re-runs readiness when a live case resolves. New tickets reach the workspace through `onboard ingest --refresh`.
- RCA runs on more cases in shadow, so it costs more model tokens, within each case's existing budget and the job's limit per run.
- The shadow replay classifies incidents the lab skips, so a new system's first readiness run makes more model calls.
- Page lookups and case memory are left out of the replay: past pages can't be looked up, and neither one changes the routing decision.
- Grouping and dedup are measured on tickets only. Alert storms aren't in the histories.
