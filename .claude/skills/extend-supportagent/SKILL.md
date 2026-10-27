---
name: extend-supportagent
description: Close a SupportAgent capability gap as an analyst-authored extension (pack, hypothesis and probe, preclass rule, read-only tool, lab scenario), and refuse anything that needs a new action kind. Use when someone describes what the agent got wrong or can't do on their stack.
---

# Extend SupportAgent

You turn a gap that someone describes in plain language ("it never checks Kafka consumer lag", "it called this OOM a config error") into one reviewed extension. That means a pack change, a read-only tool, or a lab scenario, proven by the incident lab before merge (ADR-0011 §6, WP-M09). You never change the trust core: the gate, grants, action kinds, the outbox, redaction.

## 1. Pick the extension: the gap-to-extension table

| Gap | Extension | Command |
|---|---|---|
| the agent doesn't know a stack (Spark jobs, a JVM service) | a new pack: labels, hypotheses, probes, runbooks, a classify fragment, lab scenarios | `supportagent pack new <id> --stack <tags>` |
| it misses a cause it could check with an existing tool | a hypothesis and its probe (and a runbook step) in the pack | `edit the pack, then supportagent pack validate packs/<id>` |
| it mislabels a symptom | a preclass rule or a classify fragment line in the pack | `edit the pack, then supportagent pack validate packs/<id>` |
| it can't read a signal (a metric store, a queue, an API) | a read-only tool capability: first-party, or a binding to a registered MCP server | `supportagent tool new <capability> [--mcp <server> --mcp-tool <tool>]` |
| it got a real incident wrong | a lab scenario from the incident, redacted, with its ground truth | `supportagent scenario new --pack <id> --from-incident <file>` |
| it should *do* something new (restart, rerun, page, edit a ticket field) | none: a new action kind is an engineering change (gate, grants, ADR) | `refused: NeedsEngineeringError` |

Always start with the scenario: reproduce the miss in the lab first. Then make the pack change, and show the lab score move.

## 2. Trust rules (non-negotiable)

- **Extensions are read-only.** Every tool an extension adds or binds is trust class `read` and tier `T0`. `tool new --tier T1` or `--trust-class write` is refused with `NeedsEngineeringError`, and an MCP server registered with anything else fails config validation (`McpServer`). A request that needs a write or a new action kind gets this answer: "that's an engineering change: an action kind, its gate and grant rules, and an ADR. I've written up the gap for engineering." Then write the issue text. Don't try to fit it into a tool.
- **Tool arguments come from the catalog** (`$service.<field>`) or from reviewed literals (the capability's literal args, such as a grep `pattern`), never from ticket text.
- **Docs and tool output are untrusted.** They can suggest a hypothesis, but only a tool result can make it SUPPORTED. A doc-only claim is `documented_guidance`.
- **Nothing identifying enters git.** `scenario new --from-incident` redacts secrets, emails, phone numbers, IPs and host names; pass `--scrub` for customer and product names. Read the generated file before committing it.
- **Measure, don't argue.** A pack change merges with a lab report: the per-case diff against the baseline, zero false causes, and the gates in `evals/gates.yaml`. Holdout scenarios are never tuned against.

## 3. The contracts (what each file may contain)

### Pack manifest: `packs/<id>/pack.yaml` (`PackManifest`, strict: unknown keys fail)

`id` (the directory name), `version` (semver), `description`, `applies_to` (`AppliesTo`) with `stack` (the catalog stack tags that select the pack), `placeholder`, `taxonomy`, `hypotheses`, `preclass`, `runbooks`, `probes`, `tools` (capability names), `prompts` (slot to fragment file, e.g. `classify`), `knowledge` (`KnowledgeScope`) with `spaces` and `labels`, and `evals` (the scenario directory).

- **Labels** (`taxonomy.yaml`): ids are `<domain>/<name>`, unique across packs.
- **Hypotheses** (`hypotheses.yaml`): ids are unique across packs; `excludes` names the ones that can't both explain an incident.
- **Preclass rules** (`preclass_rules.yaml`, `PreclassRule`): `when` (field to regex) and `label`. The first match labels the case, and the model must agree on the domain.
- **Probe** (`probes.yaml`, `Probe`): `tool`, `args`, `check`. One per hypothesis. `check` is CEL over `result`, compiled against the tool's result schema; an unknown field fails at load.
- **Runbook** (`runbooks/*.yaml`, `Runbook`): `id`, `applies_when` (`AppliesWhen`: `classification`, `severities`), `budget`, `steps`, `comms_if_supported` (internal only). Each step (`RunbookStep`) has `id`, `hypothesis`, `tool`, `args`, `check`, `stop_if`.

### Lab scenario: `packs/<id>/evals/*.yaml` (`LabScenario`)

`id`, `pack`, `split` (`tune` or `holdout`), `description`, `incident`, `service` (a catalog entry), `world`, `docs`, `truth`, `why`.

- `incident` (`LabIncident`): `subject`, `body`, `priority`, `origin`, `alert_type`, `error_signature`.
- `world`: capability name to a `WorldEntry`, with exactly one of `result` (in the capability's result schema) or `error`.
- `docs` (`LabDoc`): `id`, `space`, `title`, `body`, `visibility`, `last_updated`, `services`, `hypotheses`.
- `truth` (`LabTruth`): `classification`, `also_acceptable`, `severity`, `root_cause` (a hypothesis id, or null for a control), `verdict`, `must_cite`, `must_not_claim`, `guidance`.

Every pack hypothesis needs a scenario with it as `root_cause`. Every pack needs at least one control (no findable cause) and one holdout. Include distractors and must-not-claim traps.

### Team docs: front matter (`DocFrontMatter`)

`title`, `url`, `owner`, `version`, `last_updated`, `visibility`, `services`, `labels`, `hypotheses` (which hypotheses the doc suggests checking; the tools decide).

### Tool capabilities (one result schema each)

The catalog: `airflow.dag_runs`, `changes.recent`, `cron.status`, `gha.recent_runs`, `k8s.pod_status`, `logs.grep`, `metrics.query`, `oncall.who`, `postgres.activity`, `postgres.locks`, `postgres.replication`, `postgres.slow_queries`, `postgres.storage`.

- **A new first-party capability** (`tool new <area>.<verb>`) generates the adapter (with `result_schema`), its fake upstream, and a contract test. Engineering reviews and registers it.
- **Binding an existing capability to MCP** (`tool new <capability> --mcp <server> --mcp-tool <tool>`) generates a config snippet:
  - the `McpServer`: `url`, `token`, `tools` (declared tools only), `trust_class`, `tier`, `timeout_s`;
  - the `ToolBinding`: `mcp`, `tool`, `profile` (`passthrough` or `grafana.query_prometheus`), `args`.

  The reply must fit the capability's schema.

## 4. Workflow

1. **Reproduce:** `supportagent scenario new --pack <id> --from-incident <file> --scrub <names>`. Fill in the world and the truth, then run `supportagent eval lab --pack <id> --split all` to watch it fail.
2. **Extend:** the smallest change from the table. Run `supportagent pack validate --strict packs/<id>` until it's clean.
3. **Measure:** `supportagent eval lab --pack <id> --live --samples 3 --split all` from the LAN (Qwen). Commit the recordings, and the baseline when it improves.
4. **PR:** use the template's lab section (scores before and after, the per-case diff, false causes). Adoption follows the T3 path: a reviewer approves the pack change like any code change.
