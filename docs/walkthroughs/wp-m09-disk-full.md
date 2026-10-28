# Walkthrough: "it doesn't notice when the Postgres disk is full" (WP-M09)

The WP-M09 acceptance walkthrough. A gap described in plain language becomes a pack change that the lab measures, using only the analyst tools and the [extend-supportagent](../../.claude/skills/extend-supportagent/SKILL.md) skill.

## 1. The gap, in plain language

> Orders writes failed with `No space left on device`. The agent called it a database failure but found no cause; nobody had told it to look at the data volume.

The skill's table maps this to: **it misses a cause it could check with an existing tool**. The extension is a hypothesis plus its probe and a runbook step in `python-postgres`, and the signal is the node exporter's filesystem metrics through `metrics.query`. No new tool or action is needed.

## 2. Reproduce it as a lab scenario

```
supportagent scenario new --pack python-postgres --from-incident inc0107.json --split tune
```

The generated file was redacted on the way in: the on-call email became `<EMAIL_1>`. Running the walkthrough found a bug: the host-name scrub ate the error signature `psycopg.errors.DiskFull`. It was fixed in WP-M09 by requiring host names to end in a domain suffix, and the fix has a test.

The ground truth was filled in by hand: `database/failure`, P1, root cause `pg_disk_full`, SUPPORTED, must cite `metrics.query`, and `pg_table_bloat` as a trap. The database is large but not bloated. The result is [`packs/python-postgres/evals/pg-disk-full.yaml`](../../packs/python-postgres/evals/pg-disk-full.yaml).

`supportagent pack validate packs/python-postgres` then reported the gap:

```
problem: scenario pg-disk-full: unknown hypothesis id(s) ['pg_disk_full']
```

**Before** (the oracle lab: correct classification and no planner, so only the pack's knowledge is under test): **19/20**. `pg-disk-full` came out `INCONCLUSIVE`, root cause `None`, with no runbook for `database/failure`.

## 3. Extend the pack

- `hypotheses.yaml`: `pg_disk_full`.
- `probes.yaml` and the new `runbooks/pg-failure.yaml`: `metrics.query` for the free ratio of the data volume, `check: double(result.max_value) < 0.02`. Bloat is the second step.
- `preclass_rules.yaml`: `DiskFull|No space left on device|could not extend file` maps to `database/failure`.
- `classify.md`: writes failing while reads work means `database/failure`.
- `pack.yaml` 1.1.0 becomes 1.2.0.

`supportagent pack validate --strict packs/python-postgres` reports `ok`.

## 4. Measure

- **After (oracle): 20/20**, with zero false causes. The trap held: bloat was checked and came back negative.
- **Live (Qwen `qwen/qwen3.8-27b`, `--samples 3 --split all`, 2026-10-28): 20/20**, with zero false causes. Classification and root-cause lower bounds are 0.839 (the gates are 0.7 and 0.6); before the pack change they were 0.832 over 19 cases. No regressions against the baseline, and `common` is unchanged at 5/5. The pack change re-recorded every python-postgres request, as expected.
