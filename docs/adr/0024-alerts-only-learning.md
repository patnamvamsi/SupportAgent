# 0024. An alerts-only system learns from outcomes a person records on the case

- **Status:** accepted (2026-11-28; drills count, flagged; an outcome may be recorded while alerts fire)
- **Date:** 2026-11-28
- **Code PR:** #204 (part 4), the ticketless-outcomes PR (parts 1–3)
- **Spec sections changed:** ARCHITECTURE.md §8.3 (a ticketless `outcome` moment), next-steps 1.3 ("learning live")

## Context

ADR-0022 §7.4 says that on the proving ground "the outcome calibrates the label". An alerts-only system has no tickets (#161), and today every path that learns starts from a ticket:

- **Outcome.** The `outcome` feedback moment (ARCH §8.3) runs when a ticket resolves. It takes the human-set category and priority from the ticket's diffs. An alert case has no ticket. A resolved alert is noted on the case, which never closes (#156).
- **Calibration.** `LiveProof` reads resolved cases that have a `ticket_number`, and readiness counts one only once onboarding maps that ticket to a history class.
- **Readiness.** Classes come from incident history. With no history there are no classes, so `class-not-ready` holds back every label.

On the proving ground (27 Nov) the first real alert, a container stopped as a drill, opened the right case: sentiment, P1 `runtime/down`. Its RCA was SUPPORTED (container down; host restart and crash loop ruled out). It was still a safe default, held back three ways:

1. the fitted calibration has no labels, so the score is 0 (`low-calibrated-score`);
2. with no pre-classifier label, a P1 is capped (`unconfirmed-high-severity`). This was because the pack's rules read only the body, and a detector's summary arrives as the subject;
3. readiness has 0 classes (`class-not-ready`).

As things stand, nothing an alerts-only system sees can ever lift these, however many alerts it handles.

## Decision

1. **A ticketless outcome.**
   - On a case with no ticket, a person records the outcome on the case page and resolves the case. The outcome has these parts:
     - the cause's label: one of the service's taxonomy labels, prefilled with triage's;
     - the severity;
     - when the RCA supported a cause, whether it was the real one (required);
     - whether the case was a drill.
   - It writes the same `outcome` feedback row and event as a ticket's resolution. It also gives the shadow record its human labels and agreement, filling only what it lacks.
   - Only a person can record it:
     - the agent never records an outcome;
     - an alert resolving is not an outcome;
     - it takes a signed-in person with the `record_outcome` permission (analyst and up) over the case's service.
2. **Calibration from those outcomes.**
   - `LiveProof` also returns resolved alert cases that have a recorded outcome. Each one is one `fit_calibration` observation: `(predicted, preclass, correct)`, where correct means `prefix_match(outcome label, predicted)`.
   - These go into the same `agree` and `abstain` rows as ticket outcomes. A conflict is still not evidence.
   - **The floor doesn't change:** a label needs a Wilson lower bound of at least `min_score` with `n ≥ 10`, which takes about 16 correct outcomes per row.
3. **Readiness classes for alerts-only systems.** With no incident history, each recorded outcome label is a class, and its incidents are the resolved alert cases recorded with that label:
   - the RCA found the cause when the person said its cause was the real one, and it named a false cause when they said it wasn't;
   - the class's share is its share of those cases;
   - shadow's gate re-decides each case in ARCH §4 order from the conflict check on: triage's label and the pre-classifier's are scored with this run's calibration, and the severity cap applies. The model isn't asked again.
   - The gates are the same as for history classes: the bar, 0 false causes, and shadow routing 75%.

   So a label routes only after people have confirmed it often enough and the RCA held on those cases.
4. **Pre-classifier rules read the alert's subject.** Alertmanager and Grafana put an alert's summary in its subject. The docker-compose pack labels detector summaries as follows:
   - "container is not running" and "keeps restarting" → `runtime/down`;
   - "no log lines for", "nothing published for" → `runtime/stalled`.

   With that label, the model's label is cross-checked, and a P1 is no longer capped for having none.

**Decided with the maintainer (2026-11-28):**
- **Drills count.** An induced failure is a real run of the real pipeline on the real system. Once a person records its outcome, it counts toward calibration and is flagged `drill`.
- **Timing.** An outcome can be recorded while the case's alerts still fire: a person can know the cause before recovery, and resolving the case is their decision.

## Alternatives considered

- **Calibrate from the pack labs or System D.** Rejected. Their scenarios are authored with known answers. Counting them as holdout evidence for a real system is exactly what ADR-0002's grade rules prevent, and the floor stays (the maintainer's decision, next-steps 1.3).
- **Self-grading:** the alert resolved and the RCA was SUPPORTED, so the label counts as correct. Rejected: the model would grade itself, and the RCA checks the cause, not the label.
- **A lower floor for alerts-only systems.** Rejected for the same reason as the labs.

## Consequences

- **Better:** an alerts-only system has a path to routing with no ticket system. The evidence is a person's judgement on that system's own incidents, and the same rows, floors and gates as everywhere else apply.
- **Worse: it is slow.**
  - The proving ground had one alert, a drill, in 6 days. About 16 confirmed outcomes per label means months, unless drills count (open question).
  - Shadow mode is unaffected: it keeps recording what it would have done.
- **Tests:**
  - part 4: `test_a_detector_alert_is_pre_classified_by_its_summary`;
  - parts 1–3: `test_a_person_resolves_a_ticketless_case_with_its_outcome`, `test_only_a_ticketless_case_and_a_permitted_person`, `test_a_label_routes_once_enough_outcomes_confirm_it` (15 confirmed outcomes don't clear the floor; 16 do), `test_a_cause_the_person_rejected_is_a_false_cause`, `test_redecide_keeps_triage_order_and_the_severity_cap`;
  - System D's end-to-end test resolves each incident's case on its page and checks that the next onboarding run builds one live class per recorded label.
- **Watch:** how many outcomes each label still needs. Status already says this for ticket systems and will say it here too.
