# Skill packs

A skill pack is one technology stack's failure knowledge (ADR-0011 §1). A catalog service declares its `stack:` tags; for each case the agent uses the `common` pack plus every pack whose `applies_to.stack` shares a tag.

| Pack | Status |
|---|---|
| `common` | Built from `taxonomy/` and `runbooks/` (no manifest). Always active. |
| `python-postgres` | 5 labels, 12 hypotheses, 7 preclass rules, 9 runbooks, 15 lab scenarios (3 held out). Its tools ship with WP-M04. |
| `spark`, `snowflake` | Placeholders: loaded and reported, never selected. |

## Manifest

```yaml
id: python-postgres            # matches the directory name; 'common' is reserved
version: 0.1.0
description: ...
applies_to: {stack: [python, postgres]}
placeholder: false             # true: never selected
taxonomy: taxonomy.yaml        # same shape as taxonomy/classification.yaml
hypotheses: hypotheses.yaml    # same shape as taxonomy/hypotheses.yaml
preclass: preclass_rules.yaml  # same shape as taxonomy/preclass_rules.yaml
runbooks: runbooks/            # runbook YAMLs, compiled against tool schemas
probes: probes.yaml            # one probe per hypothesis
tools: [postgres.activity]     # capabilities; unknown ones are reported as unbound
prompts: {classify: classify.md}   # fragments, at most 2000 characters each
knowledge: {spaces: [], labels: []}   # WP-M08: knowledge spaces this pack's cases may read
evals: evals/                  # lab scenarios (WP-M02); absent: "not evaluated"
```

Every file is optional and relative to the pack directory. Label, hypothesis and runbook ids must be unique across all packs, and every preclass rule must name a label some pack defines. `supportagent doctor` reports each pack, which services use it, and whether it is evaluated.

## Precedence

When a stack pack and `common` both have a runbook for a case's label, the stack pack's runbook is used. A pack adds labels, rules and hypotheses to `common`'s; it never replaces them.

## Knowledge vs model

`tests/unit/test_python_postgres_pack.py` runs every scenario with an oracle that classifies correctly and plans nothing, so it tests only the pack's knowledge (runbook order, checks, thresholds). `supportagent eval lab --pack python-postgres --live` then measures the model on top.

## Team docs (WP-M08)

A pack's `knowledge.spaces` and a catalog service's `docs:` name the knowledge spaces RCA may search for a case. A doc can name hypotheses in its front matter (`hypotheses: [pg_slow_query_regression]`); when a search returns it, those hypotheses' probes join the plan, and the tools decide. A doc never makes a claim SUPPORTED on its own. Lab scenarios carry `docs:` fixtures (helpful, wrong-cause, stale and injected), with `truth.guidance` naming the docs a good search should return.

## Extending (WP-M09)

An analyst closes a gap with a generator, then proves it in the lab. The gap-to-extension table, the trust rules and every contract are in the project skill [`.claude/skills/extend-supportagent/SKILL.md`](../.claude/skills/extend-supportagent/SKILL.md).

```
supportagent pack new kafka-jvm --stack kafka,jvm --description "Kafka consumers on the JVM"
supportagent scenario new --pack kafka-jvm --from-incident incident.json --scrub "Acme"
supportagent pack validate --strict packs/kafka-jvm
supportagent eval lab --pack kafka-jvm --live --samples 3 --split all
```

Extensions are read-only: a tool is trust class `read`, tier `T0`, and a request for a new action kind is refused and routed to engineering.
