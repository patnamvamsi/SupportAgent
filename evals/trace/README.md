# Trace lab (ADR-0021 §6)

One directory per flow fixture: a reviewed `flow.yaml` and its labelled
`scenarios.yaml`. Each scenario holds one object's log lines across the flow's
components, the moment it is asked about, the log stores that are down, the
personal data seeded into its lines, and the truth (the verdict, the stage, the
late stages).

`supportagent eval trace` traces every scenario with the real tracer and
explainer and gates on:

| Gate | Rule |
|---|---|
| `wrong_stage` | none: claiming an object stopped somewhere it didn't (or isn't stopped) is the trace's false cause. "Can't tell" never counts as one |
| `deviation_found` | at least 90% of stuck objects found stuck at the right stage |
| `cannot_tell_names_stage` | every can't-tell names its stage and why |
| `pii_leaks` | none: no seeded value in any model request |
| `answers_kept` | the share of scenarios where a business sentence survived its check |

The fixtures are regression tests for the generic tracer, never per-system
code. A real system's flows get their own scenarios once a person has
labelled its questions.

- `system-c/`: System C's shipment flow (four services, 25 scenarios).
  Regenerate the scenarios with `python evals/trace/generate_scenarios.py`.
- `recordings/`: the model's answers, replayed in CI. Re-record with
  `supportagent eval trace --config evals/lab/lab.yaml --live`.
