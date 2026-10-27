# 0011. A modular framework: skill packs, tool bindings, connectors, knowledge, evals that gate them, and analyst-authored extensions

- **Status:** accepted (direction). Delivery is tracked as WP-M01 to WP-M09 in [14-wp-modular.md](../tech-spec/14-wp-modular.md).
- **Date:** 2026-10-27
- **Code PR:** this ADR and the plan first; each WP lands in its own PR.
- **Spec sections changed:** ARCHITECTURE.md §3 (component overview), §5 (RCA hypotheses and plans), §7 (tools), §11 (evals), §12 (taxonomy); ROADMAP.md §3 (sequencing); 02-interfaces.md §1 (config), §6 (ticket ports)

## Context

The trust core works. The staging exit demo on a real ServiceNow developer instance (2026-10-25) ran the gate, outbox, comms plans, takeover, redaction and replay end to end. But the agent's *capability* is generic and thin, and the integration surface is hard-wired:

- **One generic loop for every application.** RCA knows 6 hypotheses, 1 runbook (`db-pool`) and 5 tools (logs, Airflow, GitHub Actions, cron, on-call). A Python web service on Postgres, a Spark job and a Java microservice all get the same hypotheses and checks.
- **Capability is unmeasured.** The smoke suite (24 synthetic, one-signal cases) proves wiring, not triage quality. RCA-lite has never run against a real LLM: no route opts into `rca: lite`, and its slots are unconfigured. There is no RCA eval.
- **ServiceNow leaks into the core.** About 28 references in `app/`: `SnTable`, `sys_id`, `JournalField`, priority numbers, state codes, and takeover rules written against `sys_audit` field names. Another incident tool would mean editing triage, comms, sync, takeover and feedback.
- **No place for documentation.** Teams keep runbooks and SOPs in Confluence or Git. The agent can't use them, and nothing says how it safely could.

The product owner decided that the framework should be modular by technology stack and by incident tool. The first skill pack is **Python microservices and web servers on Postgres**, with placeholders for Spark and Snowflake. Capability must be proven by evals before it is trusted.

## Decision

### 0. What never becomes pluggable: the trust core

Intake normalization, dedup and storms, the classifier's decision branches (ARCH §4), the INV-01 gate, the outbox and INV-04 reconciliation, redaction (INV-06), the event log and replay, takeover semantics, RBAC and calibration stay in the core. **Plug-ins add capability, never authority.** No plug-in can add an action kind, authorize a send, or bypass redaction.

### 1. Skill packs: knowledge per technology stack

A **skill pack** is a versioned bundle under `packs/<id>/`:

```yaml
id: python-postgres
version: 1.0.0
applies_to: {stack: [python, fastapi, flask, django, gunicorn, uvicorn, celery, postgres]}
taxonomy: taxonomy.yaml        # labels this pack adds (namespaced, e.g. db/pool_exhaustion)
hypotheses: hypotheses.yaml    # pack hypotheses (ids namespaced, excludes allowed)
preclass: preclass_rules.yaml  # cheap signals ("psycopg.OperationalError", "WORKER TIMEOUT")
tools: [logs.search, postgres.activity, postgres.locks, postgres.slow_queries,
        postgres.replication, postgres.storage, metrics.query, k8s.pod_status, changes.recent]
runbooks: runbooks/            # typed plans (ARCH §5.1), validated against tool schemas
prompts: {classify: classify.md, propose_hypotheses: propose_hypotheses.md}   # fragments
knowledge: {spaces: [platform], labels: [postgres, python]}   # reserved, see §4
evals: evals/                  # lab scenarios with known root causes, see §5
```

- **Selection.** A catalog service declares `stack: [...]`. For each case, the packs whose `applies_to` matches are active, and only theirs reach the prompts and the RCA planner. `common` is always active.
- **Composition is validated at load**, as runbooks are today: ids unique across active packs, every runbook's tools bound, checks compiled against tool schemas, prompt fragments within a size budget.
- **Today's knowledge moves into packs.** The 6 hypotheses and the generic probes become `common`; `db-pool` moves to `python-postgres`.
- **Placeholders.** `spark` and `snowflake` ship as manifest stubs with no scenarios. They load, and `doctor` reports them as *not evaluated*, so no service can enable them in assisted mode.

### 2. Tool bindings, with MCP as a backend

Packs name **capabilities** (`postgres.activity`, `metrics.query`). A deployment **binds** each capability to a backend:

```yaml
tools:
  postgres.activity: {first_party: postgres, dsn_env: PG_READONLY_DSN}
  metrics.query:     {first_party: prometheus, base_url: http://prometheus:9090}
  logs.search:       {mcp: grafana-loki, tool: query_logs}
```

- Every capability has one result schema, so pack checks compile once and run on any backend.
- MCP servers plug in through the WP-204 registration contract, pulled forward from v0.2: declared tools only, trust class fixed at registration, output treated as untrusted data, read-only unless a tool is explicitly tiered and gated.
- A capability with no binding is skipped. A hypothesis whose checks can't run can never be SUPPORTED (ARCH §5.5).

### 3. Connectors: the incident system is a plug-in

The ticket ports become a connector contract with a normalized model and a capability manifest. ServiceNow becomes the first connector.

| Today (ServiceNow-specific) | Connector contract |
|---|---|
| `SnTable`, `sys_id` | `record_type` (incident, request, alert), `external_id` |
| `JournalField.WORK_NOTES` / `COMMENTS` | `NoteVisibility.INTERNAL` / `CUSTOMER` |
| priority 1-5, state 6/7/8 | the connector maps them to `Severity` (ADR-0009) and to lifecycle states |
| takeover rules on `assigned_to` / `state` audit rows | normalized changes (`assignee_changed`, `status_changed`) with a classified actor; the takeover rules stay in the core |
| ServiceNow `doctor` checks | each connector ships its own checks |

The manifest declares what the system supports (history, internal notes, idempotency markers, creation audit). The core degrades safely when a feature is missing. For example, a connector without change history gets no automatic takeover detection, so every write proposal needs a human approval. A second connector (Jira Service Management, Zendesk, PagerDuty or Opsgenie incidents) is built only when a customer needs it.

### 4. Knowledge (RAG): reserved now, built after the first pack has a baseline

Documentation enters as a fourth plug-in type, a **knowledge source** (Git markdown, Confluence, SharePoint, or an MCP server). These rules are fixed now, so packs and the catalog reserve the fields:

- **Guidance, never evidence.** Retrieved docs can raise a hypothesis's priority and suggest checks. SUPPORTED still needs tool evidence; a claim backed only by a doc is labelled *documented guidance*.
- **Never authority.** An SOP is mapped onto the existing typed action kinds, and each action passes the gate. Doc text is never executed.
- **Untrusted input.** Docs pass redaction (INV-06) at ingest and the injection-canary handling at use.
- **Cited and replayable.** Chunks carry page id, version, URL, owner, last-updated and a content hash. The trace records which chunks were read.
- **Scoped.** Packs (`knowledge:`) and catalog services (`docs:`) scope retrieval. Visibility labels follow the doc, so an internal page never reaches a customer draft.
- **`knowledge.search`** is a read-only tool capability: BM25 first (case memory already has it), then hybrid search with a local embedding model.

### 5. Evals: reliability is measured in six layers

| # | Layer | What it proves | Where it runs |
|---|---|---|---|
| 1 | Invariants (INV-01..06, both engines) | it can't act without authority, leak, or double-send, whatever the model does | every PR, hard gate |
| 2 | Contracts (each connector and tool binding, fake vs real; recorded fixtures; MCP schema conformance) | the plugs behave like the real systems | every PR |
| 3 | **Pack evals: the incident lab** | it finds the right root cause for this stack | every PR from cached LLM responses; nightly live (N samples) |
| 4 | Scenario simulation (the full stack on fake connectors and tools, with faults) | the whole loop stays safe under storms, takeovers and outages | nightly |
| 5 | Calibration (layer 3 feeds `calibrated_score` per label) | when it's unsure, it hands the case to a human | with every layer-3 baseline |
| 6 | Production feedback (shadow agreement, human diffs) | it stays right on live traffic; misses become new lab cases | weekly review |

A **lab scenario** is an incident, the *world* the tools will return (logs, metrics, database state, changes, docs), and the ground truth: classification, severity, root-cause hypothesis, expected verdict, required citations and must-not-claim traps. Scenarios include distractors (an unrelated deploy), cases with no findable cause (the right answer claims none: CONSISTENT_UNCONFIRMED when every check ran negative, INCONCLUSIVE when the investigation couldn't finish), prompt-injection content in logs and docs, and stale docs.

Scoring is **structural and deterministic**. Typed outputs are compared with the ground truth. LLM-as-judge is used only for the wording of customer updates. Gated metrics:

- classification accuracy;
- severity correctness on the ARCH §11 cost matrix;
- root-cause top-1;
- **false-cause rate** (SUPPORTED on the wrong hypothesis; the strictest gate);
- correct abstention;
- citation faithfulness;
- tool calls, tokens and latency per case.

Stochastic runs use N samples, a per-case majority and Wilson lower bounds over cases (ARCH §11). A held-out split (`evals/splits.yaml`) is never used for tuning. **A pack can't be enabled for assisted mode until it passes its gates**, and any prompt, pack or model change runs the full lab with a per-case diff before merge.

### 6. Analyst-authored extensions: describe the gap, get a reviewed extension

A support analyst who spots a gap shouldn't need to learn the framework or act as a developer. They describe the gap and the intended automation in plain language, ideally with one or two real incidents as examples, to a coding harness (Claude Code). The harness turns it into an extension that fits the framework:

1. **Classify the gap**, which decides the extension type:

   | The analyst says... | Gap | Extension |
   |---|---|---|
   | "It never thinks of X as a cause" | missing knowledge | pack hypotheses, preclass rules, a runbook |
   | "It can't see Y" (a dashboard, a query, a system) | missing evidence | a tool capability: a first-party adapter or an MCP binding |
   | "We always check Z the same way" | a known procedure | a runbook (later: distilled from the team's docs) |
   | "It should *do* W" (restart, re-run, update) | missing action | **not an extension:** a core change for engineering (a new action kind, tier and gate path) |

2. **Scaffold** with deterministic generators (`supportagent pack new`, `tool new`, `scenario new`). The harness fills the templates; it doesn't invent structure.
3. **Turn the analyst's incidents into lab scenarios** with ground truth. The extension must solve them, and must not regress the pack's existing scenarios.
4. **Validate and evaluate:** load-time validation (`pack validate`), then the lab for the pack (`eval lab --pack <id>`), with a per-case diff.
5. **Open a pull request** carrying the eval report. Adopting it is a **T3 change** (CAP §2): eval-gated, human-reviewed, versioned, with one-command rollback.

The harness learns the conventions from a **project skill** in the repo (`.claude/skills/extend-supportagent/`), which encodes the pack, tool and scenario contracts, the trust rules and the gap-to-extension table. The CLI generators and validators make the result correct by construction rather than by the harness's memory.

**Guardrails:** an extension adds knowledge or read-only evidence only. Anything that needs a new write action, a new authorization path or a redaction exception is refused and routed to engineering as a core change.

## Consequences

- **Better:**
  - prompts and plans see only the active stack's hypotheses, which should *raise* accuracy;
  - capability work is measured, not guessed;
  - adding a stack is adding data and tool bindings, not editing the core;
  - a second incident tool doesn't touch triage or the gate;
  - an analyst can close a capability gap without learning the codebase, and every such change arrives with its own evidence.
- **Worse:**
  - more surface to validate at load;
  - each pack carries its own eval suite to maintain;
  - the connector refactor touches most of `app/`, so it is sequenced last and done behind the existing contract suites.
- **Risks:**
  - lab scenarios are written by us, so they can flatter the agent. Mitigations: distractors, a held-out split, LLM-generated variants reviewed by a person, and the partner holdout (BL-037) replacing or extending the lab when it arrives;
  - MCP servers and docs widen the injection surface. Mitigations: untrusted-data handling, read-only defaults and canaries in the lab.
- **Deferred, not dropped:** the ServiceNow items parked during the pivot (the exit demo's fake leg, BL-046) and the v0.2 plan. WP-204 (MCP) moves into WP-M06.
- **Watch:** per-pack lab scores against the baseline band; false-cause rate above all; and the token cost of composed prompts.
