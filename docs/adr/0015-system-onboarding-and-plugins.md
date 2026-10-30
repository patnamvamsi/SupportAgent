# 0015. System onboarding: an instance learns the system it supports, asks for what it lacks, and proves readiness; everything pluggable is a plugin

- **Status:** accepted (2026-10-29, with the maintainer's answers below)
- **Date:** 2026-10-29
- **Code PR:** this ADR first; delivery as WP-O1 to WP-O8 (below), each in its own PR
- **Spec sections changed (when accepted):**
  - ARCHITECTURE.md §3 (components: onboarding, system graph), §5 (graph-aware hypotheses), §7 (tools via plugins), §11 (the onboarding lab), §12 (taxonomy from plugins);
  - 02-interfaces.md §1 (`system.yaml`, `plugins:`), §2 (onboarding API), §4 (`onboard` CLI);
  - 03-data-and-migrations.md (graph provenance, onboarding requests).
- **Builds on:** ADR-0011 (packs, capability bindings, MCP, connectors, knowledge sources, evals), ADR-0013 (connector seam), ADR-0014 (SOPs).

## Context

Today an instance knows an application only through what a person writes into its config and catalog. ADR-0011 made capability modular: packs per stack, capabilities bound to first-party adapters or MCP servers, knowledge sources. Choosing all of it is still manual: which packs, which tools, which access, which docs. There is no record of what the agent knows about the system, and no measure of whether it is ready.

The maintainer's target is two very different systems, each supported by its own instance:

| | System A | System B |
|---|---|---|
| Stack | Java microservices, Oracle, Angular UI | Snowflake, large-scale data processing, Kafka streams, a Streamlit app, PySpark |
| Typical incidents | 5xx and latency, JVM heap and GC, thread-pool and connection-pool exhaustion, Oracle locks, ORA- errors, tablespace, slow SQL, failed deploys, UI errors (CORS, bundle, API) | late or missing data, failed or stuck jobs, consumer lag, broker or partition trouble, schema drift, Spark executor OOM and skew, warehouse queuing and credits, task failures, stale dashboards |
| What "an outage" means | a request path is down or slow | data is late, wrong or incomplete. Freshness and correctness matter as much as availability |
| Shared | Confluence spaces, ServiceNow incident history, extensive project documentation, read-only production access |

Pointed at A, an instance should work out that it needs Java/JVM, Oracle and frontend know-how, plus access to APM, logs and Oracle's dynamic views. It should then ask for exactly those, set them up, and prove it handles A's real incident classes. Pointed at B, the same image should arrive at a different plugin set, different access requests and a different taxonomy. Nobody should hand-pick any of it.

## Outcomes: what "ready to support system X" means

Each outcome is an artifact a person can review, and each is measured. Nothing is "ready" because the agent says so.

| # | Outcome | The artifact | How it is measured |
|---|---|---|---|
| O1 | **It understands the system.** Components, datastores, queues and topics, jobs and pipelines, UIs, dependencies, owners, environments, and the CI-to-component map. | The **system graph**: every fact carries its source and confidence, and is proposed, confirmed or rejected. | Precision and recall against the reference systems' ground truth (the onboarding lab, WP-O7), and the share of facts SMEs confirm. |
| O2 | **It knows what goes wrong here.** The incident classes in the system's history, weighted by frequency, severity and time to resolve. | The **incident map**: clusters of past incidents mapped to taxonomy labels and known causes, citing problem records and resolution notes. | Each cluster is checked against labelled history; coverage is the share of incidents that fall in a mapped class. |
| O3 | **It knows what it is missing.** For each incident class: is there a hypothesis, can its evidence be read, is there a runbook. | The **gap report**, ranked by how much incident history each gap blocks. | It recommends the right plugins and access, measured as precision and recall on the reference systems. |
| O4 | **It asks for exactly that, at least privilege.** | **Onboarding requests**: which plugin, which access, which scope and why ("unlocks 31% of past incidents"), and how the grant will be verified. | No request exceeds the plugin's declared least-privilege contract. Each grant is verified read-only before use. |
| O5 | **It sets itself up once given.** | Plugins installed and capabilities bound; every binding verified by `doctor`. | Zero manual config edits from the plan to a working setup on the reference systems. |
| O6 | **It proves readiness, per incident class.** | The **readiness report**: history replayed as lab scenarios and in shadow mode. Per class: top-1 cause accuracy, false-cause rate, abstention, calibration. | The gates per class. Only *ready* classes route; the rest go to a human by default. |
| O7 | **It stays current.** New incident classes, changed docs, a new service. | **Drift findings** and a re-onboarding diff. | Time from a change in the system to the graph and plan reflecting it. |

## Decision

### 1. Onboarding is a pipeline with reviewed checkpoints

An instance is pointed at a system with an **onboarding manifest**. This is the "defined process" through which links and access are handed over:

```yaml
# system.yaml
system: {id: payments-platform, name: "System A", owners: [team-payments@acme.com]}
sources:
  servicenow: {business_service: "Payments", assignment_groups: [PAY-L2], history: 12mo,
               include: [incident, problem, change_request, kb_knowledge, cmdb_ci]}
  confluence: [{space: PAY}, {space: PAYARCH}]
  git: [{repo: github:acme/payments-*, paths: ["**/pom.xml", "**/application*.yml",
        "**/helm/**", "**/*.md", "angular.json", "package.json"]}]
access:            # read-only production access, handed over as it's granted
  - {kind: oracle, name: paydb, dsn_env: PAYDB_RO}
  - {kind: grafana, url: https://grafana.acme.internal, token_env: GRAFANA_RO}
budget: {llm_tokens: 20000000, wall_clock: 6h}   # reading everything has a cost
```

The pipeline runs as a resumable job: `supportagent onboard`, with `/ui/onboarding` showing each phase. Every phase writes artifacts, and a person reviews them before the dependent phases run.

```
P1 Ingest ─► P2 Understand ─► P3 Incident map ─► P4 Gap plan ─► [humans grant] ─► P5 Provision ─► P6 Prove ─► P7 Operate
   (read)      (system graph)    (history)         (requests)                       (plugins,       (readiness   (drift,
                                                                                      bindings)      per class)   re-onboard)
```

- **P1 Ingest.** Read everything in `sources`, read-only, with redaction at ingest (INV-06) and within budget. ServiceNow brings the CMDB with CI relationships, the incident, problem and change history, and KB articles. Confluence brings pages with labels and versions. Git brings manifests and docs, never source code beyond declared paths. The knowledge layer stores it all (WP-M08), with provenance.
- **P2 Understand: the system graph** (§3). Deterministic extractors run first, then LLM extraction for prose, and each extracted fact is checked against its source.
- **P3 Incident map.** Cluster the history by error signature, text and CI. Map each cluster to taxonomy labels, and mine causes from problem records and resolution notes. Weight clusters by count × severity × time to resolve.
- **P4 Gap plan** (§4). For every weighted cluster: which hypotheses would explain it, which capabilities would test them, which plugin provides them, and what access each needs. The output is a ranked list of **onboarding requests**.
- **P5 Provision.** Once a request is granted (a credential lands in the secret store, or a plugin is approved), it is installed and bound, and `doctor` checks the plugin's contract: reachable, the declared scope, **and nothing more**. A write-capable credential is refused.
- **P6 Prove** (§5). Past incidents become lab scenarios automatically, with each scenario's world drawn from the evidence that incident left behind. They are replayed through the real pipeline in shadow mode. Per incident class, this gives top-1, false-cause rate, abstention and calibration. Classes that pass their gates are *ready*.
- **P7 Operate.**
  - Shadow mode, then assisted, **per incident class**.
  - Feedback and misses become scenarios (ADR-0011 layer 6).
  - Drift detection re-runs P1 to P4 incrementally and opens a re-onboarding diff, for example a new topic, a renamed service or an incident cluster no plugin covers.

### 2. Everything pluggable becomes a plugin

ADR-0011 has four pluggable kinds: packs, capability backends (first-party or MCP), connectors and knowledge sources. This ADR adds two more, **extractors** and **detectors**, and puts all six under one **plugin** model: one manifest, one registry, one lifecycle, one trust model.

```yaml
# plugins/oracle/plugin.yaml
id: oracle
version: 1.0.0
core_api: ">=1.0,<2"
provides:
  pack:           # knowledge: taxonomy, hypotheses, probes, runbooks, prompt fragments, scenarios
    taxonomy: taxonomy.yaml
    hypotheses: hypotheses.yaml   # oracle/lock_contention, oracle/tablespace_full, oracle/ora_00060...
    runbooks: runbooks/
    evals: evals/
  capabilities:   # evidence, each with one result schema
    oracle.sessions: {schema: schemas/sessions.json}
    oracle.locks:    {schema: schemas/locks.json}
    oracle.top_sql:  {schema: schemas/top_sql.json}
  backends:       # how a capability is served
    - {capability: "oracle.*", first_party: oracle_ro}            # signed, in-process
    - {capability: "oracle.*", mcp: {image: ghcr.io/acme/oracle-mcp:1.2, tools: [...]}}
  extractors:     # discovery: what this plugin can read off a system
    - {match: "**/application*.yml", extract: datasources}       # jdbc:oracle:... -> Datastore nodes
    - {match: cmdb_ci_db_ora_instance, extract: cmdb}
  detectors:      # when this plugin applies
    signals:
      - {graph: {node: Datastore, tech: oracle}}
      - {incidents: {regex: "ORA-\\d{5}"}, min_share: 0.02}
      - {docs: {terms: [oracle, tnsnames, AWR]}}
requires:         # least-privilege access contracts, one per backend
  oracle_ro:
    grant: "CREATE SESSION; SELECT on V$SESSION, V$LOCK, V$SQL, DBA_HIST_SQLSTAT, DBA_DATA_FILES"
    verify: [can_select: V$SESSION, cannot: [insert, update, delete, ddl]]
    secret: {dsn_env: "<name>_RO"}
trust: {class: T0-read, network: [oracle-listener], data: redacted}
```

- **One plugin, several provides.** A Kafka plugin brings consumer-lag hypotheses, `kafka.consumer_lag` and `kafka.topic_health` capabilities, an MCP or first-party backend, an extractor for topic names in Spring and PySpark configs, and detectors. Today's `packs/` become plugins that provide only `pack`. `common` stays built in.
- **Detectors are how the instance knows what to ask for.** P4 scores every plugin in the registry against the system graph and the incident map. A plugin that matches but isn't installed becomes a request.
- **Least privilege is declared by the plugin, not invented per install.** A request quotes the plugin's `grant` text, and P5 runs its `verify` probes. A credential that can write fails verification and is not used.
- **The trust core stays outside plugins** (ADR-0011 §0). A plugin adds knowledge, read-only evidence and discovery. It can't add an action kind, authorize a send, or relax redaction. Any capability tier above T0-read is refused at install.
- **Isolation by kind of content:**
  - *Data-only plugins* (packs, extractor rules, detectors) are YAML and Markdown. They are validated at load and need no code trust.
  - *Code* runs either as an **MCP server**, out of process, with a container image, declared tools and a network policy, or as a **first-party, signed Python backend** shipped as a pip package (`supportagent-plugin-oracle`) and loaded through entry points.
  - No third-party code runs inside the SupportAgent process.
- **A registry, and a lab gate for each plugin.**
  - The registry is a git index (`plugins/index.yaml` in a catalog repo, or a local directory), recording each plugin's version, signature and published lab scores.
  - Installing a plugin pins its version and verifies its signature, then runs the plugin's own scenarios in this instance's lab.
  - A plugin that hasn't passed its gates here is *installed, not evaluated*. Its classes can't leave shadow mode.

### 3. The system graph: a knowledge graph with provenance, built in layers

The graph extends the existing case-memory tables (`nodes`, `edges`, WP-108) with types, provenance and review state.

- **Nodes:** `System`, `Service`, `Component` (a JVM service, a Spark job, a Streamlit app, an Angular app), `Datastore` (Oracle schema, Snowflake database), `Queue`/`Topic`, `Pipeline`/`Job`/`Task`, `UI`, `Runtime` (k8s namespace, cluster, warehouse), `Team`, `Doc`, `Runbook`, `CI`, `IncidentClass`, `Cause`.
- **Edges:** `depends_on`, `calls`, `reads`, `writes`, `publishes`, `consumes`, `runs_on`, `scheduled_by`, `owned_by`, `documented_in`, `maps_to_ci`, `fails_with`, `caused_by`, `resolved_by`.
- **Every fact** records its sources (doc id, version and quote hash; CMDB relation id; manifest path and line), its confidence, the extractor that found it, and a status (`proposed`, `confirmed`, `rejected`, `stale`).
- **The layers, in order of trust:**
  1. Structured sources: CMDB relationships, deployment manifests, configuration (datasources, topics, schedules).
  2. Live read-only discovery, once access exists: list topics and consumer groups, schemas and warehouses, deployments.
  3. LLM extraction from prose: typed JSON validated against the graph schema. Each extracted fact is checked against the passage it came from (the `entail` slot), and a fact that fails the check is dropped.
- **Conflicts** between sources, such as a doc saying one thing and the CMDB another, become review questions, not silent choices.
- **The graph is guidance, never evidence** (ADR-0011 §4). It scopes and orders RCA:
  - the dependencies of the failing component add their hypotheses (a Java service whose datastore is Oracle adds Oracle hypotheses);
  - it resolves which service an alert is about;
  - it says who owns a component.

  SUPPORTED still needs tool evidence. A graph fact is never a citation for a cause.
- **Reviewing.** `/ui/onboarding/graph` lists the proposed facts by confidence, with their sources, so SMEs confirm or reject them in bulk. Confirmed facts are what P4 and RCA rely on most.
- **Budget.** Reading extensive documentation is the costly step:
  - deterministic extractors run first;
  - LLM extraction only reads chunks the retriever ranks relevant to graph entities and incident clusters;
  - it runs within `budget`, can use a local model, and resumes after interruption.

### 4. The gap plan: from incident history to a ranked list of requests

For each incident class *c* (weight *w(c)*) and each hypothesis *h* that could explain it:

```
explainable(h)  = a plugin that provides h is installed
testable(h)     = every capability that h's checks need is bound and verified
documented(c)   = a runbook or doc is linked to c
gap value       = w(c) × (1 - best coverage of c's hypotheses)
```

Requests are ranked by the incident weight they unlock. Each request says what is needed, why, the exact scope and how it will be verified:

> **Grant Oracle read-only access to `paydb`.** It unlocks 4 incident classes, covering 23% of the last 12 months' incidents, including 7 of the 9 P1s. The scope is the `oracle` plugin's `oracle_ro` contract (quoted). It will be verified with `SELECT` on `V$SESSION`, with every write refused. Rough effort: a DBA, 15 minutes.

A gap no plugin covers becomes an **extension proposal** for the analyst flow (ADR-0011 §6), with the incidents that show it attached as candidate lab scenarios. A gap that needs a new *action* goes to engineering, as before.

### 5. Readiness is proven per incident class, from the system's own history

- **Scenarios from history.**
  - Each resolved incident with a known cause becomes a lab scenario: the ticket, the ground truth (taxonomy label and cause from its problem record and resolution notes), and a *world* built from evidence captured at the time.
  - Where no evidence was captured, the world is replayed from logs and metrics, read-only, for that time window.
  - Every scenario is reviewed before it counts, and a held-out split is never used for tuning.
- **Shadow replay.** The same incidents go through the real pipeline (`shadow import`).
- **Gates per class:** top-1 cause, false-cause rate (strictest), correct abstention, and calibration. Each class's threshold is set from this history (ADR-0003, per system).
- **Routing follows readiness.** A class that isn't ready safe-defaults to a person, and `/ui/onboarding/readiness` shows why. The instance is "ready to support" when the ready classes cover an agreed share of incident weight, for example 70% of the last 12 months. Promoting classes to assisted mode stays a human decision.

### 6. One image, many instances

Each instance is the same image. What differs is its system manifest, its installed plugins, its system graph and its calibration, all stored in that instance's database and `local/`. The A and B outcomes come only from what each instance reads.

| | Instance for A | Instance for B |
|---|---|---|
| Detected plugins | `java-spring` (JVM, Hikari, Spring errors), `oracle`, `frontend-angular`, `k8s`, `apm-grafana` (or Dynatrace or AppDynamics, whichever the docs and access show) | `snowflake`, `kafka`, `spark` (with the history server or Databricks), `streamlit`, `orchestration-airflow` (if found), `data-quality` (freshness, volume, schema drift) |
| Taxonomy it adds | `jvm/*`, `oracle/*`, `ui/*`, `api/*` | `data/freshness`, `data/correctness`, `stream/lag`, `job/failure`, `warehouse/*` |
| Typical requests | Oracle `V$`/`DBA_HIST` read; APM and log read tokens; k8s `get`/`list` on namespaces | Snowflake role with `ACCOUNT_USAGE` and `INFORMATION_SCHEMA` read on the listed databases; Kafka describe on groups and topics; Spark history server; Streamlit app logs |
| Graph highlights | service → service calls, service → Oracle schema, UI → API routes | topic → consumer job → Snowflake table → Streamlit page lineage |

B's lineage edges carry the most value: "the dashboard is stale" is solved by walking upstream through the graph, then testing each hop with its capability. That needs data-platform hypotheses (late upstream, a stuck consumer, a failed task, warehouse queuing), which the `data-quality`, `kafka`, `snowflake` and `spark` plugins provide.

## Delivery

| WP | What | Done when |
|---|---|---|
| **WP-O7** (first, red-first; *delivered*: `evals/onboarding/`, `supportagent eval onboarding`) | **The onboarding lab.** Reference systems A and B as fixtures: a fake Confluence space, ServiceNow history with CMDB, problems and changes, repo manifests, a read-only tool world, and the ground-truth graph, incident map, plugin set and requests. | O1–O6 metrics computed on both; CI gates. |
| WP-O1 (*delivered*: `plugins/`, `supportagent plugin`, `onboard detect`; the lab enforces the plugins stage) | **The plugin model.** Manifest v2 (`provides`, `detectors`, `extractors`, `requires`, `trust`). Registry and install with pinning and signatures. Today's packs migrate. Skeleton plugins `java-spring`, `oracle`, `frontend-angular`, `kafka`, `snowflake`, `spark`, `streamlit`, `data-quality`, each with detectors, contracts and a few scenarios. | Both reference systems detect the right plugin sets. |
| WP-O2 (*delivered*: `supportagent onboard ingest`, `llm.onboarding`) | **Ingestion.** `llm.onboarding`, a frontier model that onboarding uses when triage runs on a local one (decision 3); `system.yaml`; ServiceNow CMDB, problems, changes and KB; every page in a Confluence space; Git manifest paths. Budgeted and resumable. | P1 is complete on both fixtures within budget. |
| WP-O3 (*delivered*: `domain/graph.py`, prose extraction with source checks; the lab enforces the graph stage. The review UI follows in WP-O3b) | **The system graph.** Schema, provenance and review state; deterministic and plugin extractors; LLM extraction with source checks; the review UI. | Graph precision ≥ 0.9 and recall ≥ 0.8 on the fixtures. |
| WP-O4 | **The incident map.** Clustering, taxonomy mapping, cause mining, weighting. | Clusters match the labelled classes. |
| WP-O5 | **The gap plan and requests.** Scoring, requests with contracts, the UI, CLI and API; verification at provision. | The right requests at least privilege; zero manual edits from grant to bound. |
| WP-O6 | **Readiness.** Scenarios from history, shadow replay, per-class gates, routing by readiness. | A readiness report on both fixtures; classes that aren't ready safe-default. |
| WP-O8 | **Drift and re-onboarding.** | A changed fixture produces the right diff. |

A real system is the final test. The partner's ServiceNow and Confluence (BL-037) replace the fixtures' optimistic ground truth, as they do for the lab.

## Consequences

- **Better:**
  - pointing an instance at a new system becomes a guided, measured process rather than a configuration project;
  - access requests are explained and least-privilege by construction;
  - "is it ready?" has a per-class answer backed by the system's own history;
  - plugins unify five extension mechanisms into one, with one trust model.
- **Worse:**
  - a large surface: graph storage and review, a registry, signing, per-plugin contracts;
  - onboarding has a real LLM cost on extensive documentation, so budgets and local models matter;
  - SMEs have to review the graph and the scenarios; that time is the price of trusting the result.
- **Risks:**
  - *Plausible but wrong graphs from prose.* Mitigations: the layered trust order, a source check on every extracted fact, conflicts surfaced as questions, and graph facts never used as evidence.
  - *Flattering scenarios built from history.* Mitigations: a held-out split, review, and false-cause rate as the strictest gate.
  - *Plugin supply chain.* Mitigations: signatures, pinned versions, data-only by default, MCP out of process, verified read-only access.
  - *Prompt injection* through docs, tickets or tool output during onboarding. Mitigations: everything stays untrusted data (ADR-0011), and the lab carries canaries.
- **Unchanged:** the trust core, the INV-01 gate, redaction, shadow-first, and human promotion to assisted mode, now per incident class.

## Decisions on the open questions (maintainer, 2026-10-29)

1. **One instance per system.** The data model keeps a `system_id`, so a later instance can host several.
2. **Plugin distribution, as proposed.**
   - Third parties may ship data plugins (packs, extractor rules, detectors) and MCP servers. In-process Python comes only from us, signed.
   - First-party plugins live in this repo under `plugins/`. The registry is a git index that can also point at external catalogs.
3. **The onboarding model is whichever model the user chose.**
   - If the user configured both a local model and a frontier model, onboarding (ingest extraction, cluster labelling, gap reasoning) uses the **frontier** model.
   - Triage keeps its own slots.
   - Built as an optional `llm.onboarding` block (provider, key, model). When it is set, onboarding uses it; otherwise onboarding uses the triage model. The installer offers it whenever the triage model is local.
4. **SMEs review, and the review must be easy to do visually.**
   - `/ui/onboarding` shows the graph as a picture: components, datastores and topics, with proposed edges dashed and confirmed edges solid.
   - Next to the graph, each proposed fact is a card showing its source quote and a link to the source.
   - Facts can be confirmed or rejected in bulk by confidence or by source. Keyboard shortcuts move through the queue, and progress is shown as a share of facts reviewed.
   - The same pattern applies to scenarios built from history: the ticket, the inferred cause and the evidence side by side, approved with one click.
   - The UI serves its scripts itself, keeping the CSP `script-src 'self'`.
5. **Production access is used for both live discovery and triage.** Discovery stays read-only, uses only the declared capabilities, and is logged in the onboarding trace like any tool call.
6. **The readiness bar is 70%** of the last 12 months' incident weight, covered by classes that pass their gates. It is configurable per system.
