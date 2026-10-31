# The onboarding lab (ADR-0015, WP-O7)

Measures whether an instance pointed at a system understands it, before any real system is involved. There are three reference systems, built to differ in every way that matters:

| | `system-a`: Payments Platform | `system-b`: Customer Analytics | `system-c`: Shipment Tracking (ADR-0016) |
|---|---|---|---|
| Stack | Java/Spring Boot services, Oracle 19c, an Angular UI, Kubernetes | Kafka (MSK), PySpark streaming and batch on EMR, Kafka Connect, Snowflake tasks, Airflow, a Streamlit app | Node.js (Express) services and a worker, MongoDB, RabbitMQ, Kubernetes |
| Sources | 10 Confluence pages, 18 repo files, a CMDB with 10 CIs and 11 relationships, 45 incidents | 9 pages, 14 repo files, 9 CIs and 6 relationships, 42 incidents | 6 pages, 16 repo files, 9 CIs and 8 relationships, 43 incidents |
| Traps | the 2023 MySQL architecture, a retired service, a stale CMDB relationship, a page telling the AI to request `GRANT DBA` | the 2024 Redshift design, a draft page with wrong lineage, a stale CMDB relationship, a page telling the AI to request `ACCOUNTADMIN` | the 2023 PHP monolith on MySQL, a retired CI, a draft with wrong lineage, a page telling the AI to request RabbitMQ `administrator` and MongoDB `root` |

## Layout

```
system-x/
  system.yaml            the onboarding manifest (ADR-0015 §1)
  sources/
    confluence/<SPACE>/  pages with front matter (page_id, version, labels, last_updated)
    servicenow/          cmdb.yaml, incidents.yaml, problems.yaml, changes.yaml
    git/<repo>/          manifests and config: pom.xml, application.yml, helm, SQL, DAGs, app code
    live/discovery.yaml  what read-only discovery returns once access is granted
    evidence/<number>.yaml  what the read-only tools returned during a past incident (generated)
  truth.yaml             graph (nodes, edges, must_not, questions), plugins, requests
  truth_incidents.yaml   incident classes: members, labels, cause (generated)
gates.yaml               per-stage floors, forbidden ceilings, enforced_stages
generate_history.py      the seeded generator for servicenow/*.yaml and truth_incidents.yaml
generate_evidence.py     the seeded generator for system-a's evidence (from the generated history)
readiness_baseline.json  each class's accepted readiness; a ready class may not regress
drift/<system>.yaml      the system some time later (a change set) and the diff it must produce
```

**Every truth fact cites the sources it can be derived from** (`confluence:<page_id>`, `git:<repo>/<path>`, `cmdb:<sys_id>`, `live:<key>`), and the lab checks that each reference resolves. The truth never asks for something the sources don't contain. `must_not` lists what a careless reader would take from a stale, wrong or injected source. `questions` lists conflicts a good onboarding raises for review instead of resolving silently.

## System C: a new stack with no core code (ADR-0016)

System C is the proof that a new stack needs only plugins. Its plugins, `nodejs`, `mongodb` and `rabbitmq`, are data:
- **Declared capabilities** `mongodb.slow_ops`, `mongodb.replset` and `rabbitmq.queues`, served by MCP servers in a deployment;
- **Declarative extractors** for `package.json`, `config/*.json` and `.env.example`;
- **Two CMDB classes**;
- **Three packs**, each evaluated on its own lab.

They landed in a PR that changed nothing under `src/`. Every stage the lab enforces passes on System C, and its readiness is replayed from its own evidence.

## One command for any system (ADR-0017)

```bash
supportagent onboard run --system system.yaml --workspace <dir>   # re-run to continue
supportagent onboard status --workspace <dir>
```

`onboard run` takes any system through ingest, sense, graph, incidents, requests and readiness, and records each step in `onboarding.json`:
- **done** steps are skipped while their inputs (the sources and the pinned plugins) are unchanged;
- **blocked** steps are retried: a source unreachable, or the model gave no answer for part of the work;
- **failed** steps are retried too.

It ends with **what's needed from you**, ranked by the incident weight each item holds back: access to grant, plugins to provide or evaluate, evidence to capture, facts to review, sources to fix. Acting on one clears it on the next run. The reference systems test this loop; they are not where the work is.

## A real system has the same layout

`supportagent onboard ingest --system system.yaml --out <workspace>` reads a live system's ServiceNow, Confluence and Git sources into exactly this layout: redacted, within the manifest's budget, and resumable. Every stage then reads a real system the way it reads a reference one. `tests/unit/test_ingest.py` checks this by serving each reference system through fake ServiceNow, Confluence and GitHub servers, ingesting it, and requiring the same plugins to be detected.

## Running it

```bash
uv run supportagent eval onboarding          # both systems
uv run supportagent eval onboarding --json
```

Until the pipeline stages exist (WP-O1..O6), the lab proves itself. For each system:
- the fixture is self-consistent;
- the **oracle** result (the truth) passes every gate;
- the **null** result fails every stage.

As each stage lands, its PR adds it to `enforced_stages`, and from then on its gates bind the real pipeline.

## The graph stage (WP-O3)

The graph is built in three layers, in order of trust:
1. **Structured** (`adapters/files/graph_extract.py`): repository manifests and configuration, plus the CMDB's installed CIs and typed relationships.
2. **Live** read-only discovery (`sources/live/discovery.yaml`).
3. **Prose**: the `extract_graph` slot reads each current page.

A prose fact joins only if it is **grounded** (`domain/graph_prose.py`):
- its quote appears in the page word for word;
- the quote names both ends of the fact;
- both ends are known entities, or ones declared in the same answer with their own grounded quotes.

Then `domain/graph.py` merges everything:
- aliases are resolved;
- documentation relates what the other layers found but can't add an entity: a node only prose names is dropped (live, Qwen proposed a retired service, a CDN and "every service");
- where a structured or live edge already joins two nodes, a prose edge of another kind yields (`calls external:bank` loses to the configuration's `depends_on`);
- a finer edge subsumes a coarse one;
- a stale CMDB edge that a component's configuration contradicts is dropped and **becomes a question**.

The model's answers are recorded in `recordings/` and replayed in CI. A changed prompt or page misses its recording, and those pages add nothing until someone re-records them with `--live`. Recorded with `qwen/qwen3.8-27b` on LM Studio (about 40 minutes for both systems): System A scores edge precision 0.913, System B edge recall 0.963 (the re-recording missed one docs-only edge), everything else 1. Re-record when a prompt's inputs change: the known entities, with their kinds, are part of the prompt. The graph gate then fails, so the change can't slip through.

## The incident stage (WP-O4)

`domain/incident_map.py` turns the ticket history into classes, one per cause, structure first:

1. The `map_incidents` slot reads incidents eight at a time against a **catalogue**: the built-in labels and causes, plus those of the plugins the system needs (`provides.labels`, `provides.causes`). Each reading names a label, a cause or none, and quotes the ticket. A cause stays only if it is in the catalogue and its quote is in the ticket word for word.
2. Two structural rules overrule the readings:
   - incidents a person linked to one **problem record** form one class, named by the majority of their readings;
   - an incident a **change** caused is `recent_change_push`.
3. Incidents with a cause group by cause. Those without one group by label (routine requests, say) and count as not covered.

Each class is weighted by count × severity (P1 = 8 … P4 = 1) × (1 + days to resolve), and **coverage** is the share of incidents with a known cause. `supportagent onboard incidents --workspace <dir>` writes `incident_map.json` for a real system.

Recorded with `qwen/qwen3.8-27b` (about 18 minutes for both systems): pair F1 1 and cause accuracy 1 on both. System A's label accuracy is 0.911: for the portal class, the model put the cause id in the label field, so the label was dropped.

## The requests stage (WP-O5)

`domain/gap_plan.py` turns each access contract of a needed plugin into one request per target its `find` names. A request's scope is the contract's grant, word for word, so it can't ask for more than the plugin declares. Targets the CMDB lists as retired are never found.

A request **unlocks** an incident class when it serves a capability the class's cause needs: the capabilities of the plugin that declares the cause, plus that plugin's `needs` (the built-in causes have fixed needs in `BUILT_IN_NEEDS`). For a target in the graph, the class's CIs must also reach it. For example, lock contention on payment-service unlocks the PAYDB request, not the CRMDB one. Requests are ranked by the share of incident weight they unlock. A class nothing unlocks is a **gap**.

`supportagent onboard requests --workspace <dir>` saves the plan for review at `/ui/onboarding/requests` and writes `requests.json`. Approving a request records that a person will provide the credential. Binding it and running the contract's `verify_can` and `verify_cannot` probes is the plugin backend's job when it lands.

## Readiness (WP-O6)

`evals/readiness.py` replays the history, one incident class at a time, the way ADR-0015 §5 describes:

- **Scenarios from history.** Each past incident in a class of the incident map becomes a lab scenario. It carries the ticket as it was opened and the service it hit, as the system graph knows it: the stack that selects the packs, its databases and its namespace. Its truth is what onboarding mapped: the class's label, and the hypothesis that tests its cause (`oracle/lock_contention` is tested by `oracle_lock_contention`). Its world is the evidence captured at the time (`sources/evidence/`).
- **Replayed with the triage model.** The lab's real classifier and RCA run each scenario, since that model will support the system.
- **Gates per class** come from `system.yaml` `readiness:`: at least 3 scenarios with evidence, the cause found in 75% of them, and no false cause. A class whose cause no evaluated pack can test is *untestable*. An incident with a cause but no evidence isn't replayed; it is counted.
- **The bar** is the share of incident weight held by ready classes: 70% (decision 6).

System A, with the evaluated `oracle`, `java-spring` and `kubernetes` plugins:

| Class | Share | Qwen (recorded) | Why not ready |
|---|---|---|---|
| upstream provider | 27% | ready, 5/5 | |
| Oracle lock contention | 17% | not ready, 5/7 | two "captures timing out" tickets classified `network/latency` |
| HikariCP exhaustion | 14% | not ready, 3/6 | half its tickets are logged against payments-gateway, whose logs only show its dependency's 503s; the agent doesn't follow dependencies |
| JVM heap | 11% | ready, 5/5 | |
| bad deploy | 10% | ready, 6/6 | |
| tablespace full | 9% | ready, 4/4 | |
| plan regression | 6% | ready, 4/4 | |
| portal CORS or stale bundle | 4% | untestable | `angular` is a skeleton |
| no cause | 2% | ready, 3/3 | |

So the recorded triage model makes System A **63% ready, under the 70% bar**, with no false cause. With a classifier that's always right (`tests/unit/test_readiness.py`) it is 79%. The difference is the model's labels, not the knowledge. Only classes with a cause count toward the bar (ADR-0017): abstaining on noise is right, but it proves nothing the agent can support.

System B is untestable everywhere except its no-cause class, since none of its plugins is evaluated yet. `tests/unit/test_readiness.py` replays both systems with a stand-in that classifies every ticket correctly, so the knowledge and evidence are proven without a model. The lab replays the recorded triage model on top. Every replay must be recorded, and no class `readiness_baseline.json` has ready may stop being ready (`--write-readiness-baseline` accepts a new run).

`supportagent onboard readiness --workspace <dir>` does the same for a workspace, saves the report and writes `readiness.json`. From the next start:
- triage routes only the labels of ready classes; any other case safe-defaults to a person (`class-not-ready`);
- RCA never lets a claim for a cause whose class isn't ready be sent;
- `/ui/onboarding/readiness` shows each class, its share, its top-1 and why it isn't ready.

A system that has never been assessed routes as before.

**Not done yet.** Calibration per class, shadow replay through the real pipeline, and capturing evidence from live tools for a workspace's past incidents. Until that last one lands, a real workspace has no evidence, and none of its classes can be ready.

## Drift (WP-O8)

`domain/drift.py` compares two onboarding **snapshots**: the plugins needed, the graph's nodes and edges, the incident classes and their shares, and the requests. The **re-onboarding diff** lists what changed, each finding with what a person should do about it:
- a plugin newly needed or no longer needed;
- a node added, removed or **renamed** (same kind, relationships that mostly agree once one name replaces the other);
- an edge added or removed;
- a class that is new, gone, or grew by 5 points of the weight;
- a new class whose cause no detected plugin declares (**uncovered**);
- more incidents with no known cause;
- a request newly asked for or no longer needed.

`drift/system-a.yaml` is System A three months later:
- **disputes-service** ships on Spring Kafka (MSK) and PAYDB, behind the gateway;
- **customer-service** is renamed **customer-profile** in the repo, the gateway, the CMDB and live discovery (the docs still say customer-service);
- the history gains a recurring **consumer-lag** problem on disputes-service, and three incidents from base image bumps that dropped the PDF renderer's font.

The lab applies it to a copy (`adapters/files/change_set.py`), re-onboards, and requires the diff to equal `expected` exactly: the kafka plugin, the rename (not a removal and an addition), the new service, its cluster and its four relationships, the consumer-lag class, the change-caused class growing, and the `kafka_ro` request on the new cluster. The docs still say customer-service, so what they state about its schema no longer attaches: three edges are reported gone. That is a real finding, since the docs need the new name.

`supportagent onboard drift --workspace <dir>` does the same for a workspace. It compares the current run with the last accepted snapshot (`onboarding_snapshot.json`; the first run is accepted) and writes `drift.json`. `--accept` takes the new run once its findings are dealt with. The graph review already keeps each fact's decision across rebuilds and marks facts that are gone, so `/ui/onboarding` shows the same changes fact by fact.

## Changing the history

Edit an incident class in `generate_history.py` and run it again, then `generate_evidence.py`. Never edit the generated YAML by hand: a test regenerates both and compares the files byte for byte.
