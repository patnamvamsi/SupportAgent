# The incident lab

Scenarios with known root causes (ADR-0011 §5, WP-M02). Each file is one incident, the service it hit, the **world** each tool returns, and the **truth**.

```bash
supportagent eval lab --pack common                       # replay recorded responses (CI)
supportagent eval lab --pack common --live --samples 3    # call the model, record, N samples
supportagent eval lab --pack common --split holdout       # the held-out scenarios only
supportagent eval lab --pack common --live --write-baseline
```

- `common` scenarios live here; another pack's live under its `evals:` directory.
- `--live` writes each response to `<dir>/recordings/`; commit them so CI can replay. A replay that meets an unrecorded request fails closed and says so.
- `baseline.json` holds the last accepted per-scenario pass/fail; a scenario that passed there must still pass (`no_regressions`).
- Gates are in `evals/gates.yaml` under `lab:`. The false-cause rate gate is 0: a confident wrong root cause fails the run.

## Verdicts (ARCH §5)

| Verdict | Means |
|---|---|
| `SUPPORTED` | a check held: evidence consistent with that hypothesis |
| `CONSISTENT_UNCONFIRMED` | every planned check ran and none held |
| `INCONCLUSIVE` | the investigation couldn't finish (tool error, budget, conflict, no plan) |

A scenario with `root_cause: null` expects one of the last two: claiming any cause there is a false cause.
