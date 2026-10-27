# Skill packs

A skill pack is one technology stack's failure knowledge (ADR-0011 §1). A catalog service declares its `stack:` tags; for each case the agent uses the `common` pack plus every pack whose `applies_to.stack` shares a tag.

| Pack | Status |
|---|---|
| `common` | Built from `taxonomy/` and `runbooks/` (no manifest). Always active. |
| `python-postgres` | Skeleton: selectable, no knowledge yet (WP-M03, WP-M04). |
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
knowledge: {spaces: [], labels: []}   # reserved (WP-M08)
evals: evals/                  # lab scenarios (WP-M02); absent: "not evaluated"
```

Every file is optional and relative to the pack directory. Label, hypothesis and runbook ids must be unique across all packs, and every preclass rule must name a label some pack defines. `supportagent doctor` reports each pack, which services use it, and whether it is evaluated.
