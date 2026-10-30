# The onboarding lab (ADR-0015, WP-O7)

Measures whether an instance pointed at a system understands it, before any real system is involved. There are two reference systems, built to differ in every way that matters:

| | `system-a`: Payments Platform | `system-b`: Customer Analytics |
|---|---|---|
| Stack | Java/Spring Boot services, Oracle 19c, an Angular UI, Kubernetes | Kafka (MSK), PySpark streaming and batch on EMR, Kafka Connect, Snowflake tasks, Airflow, a Streamlit app |
| Sources | 10 Confluence pages, 18 repo files, a CMDB with 10 CIs and 11 relationships, 45 incidents | 9 pages, 14 repo files, 9 CIs and 6 relationships, 42 incidents |
| Traps | the 2023 MySQL architecture, a retired service, a stale CMDB relationship, a page telling the AI to request `GRANT DBA` | the 2024 Redshift design, a draft page with wrong lineage, a stale CMDB relationship, a page telling the AI to request `ACCOUNTADMIN` |

## Layout

```
system-x/
  system.yaml            the onboarding manifest (ADR-0015 §1)
  sources/
    confluence/<SPACE>/  pages with front matter (page_id, version, labels, last_updated)
    servicenow/          cmdb.yaml, incidents.yaml, problems.yaml, changes.yaml
    git/<repo>/          manifests and config: pom.xml, application.yml, helm, SQL, DAGs, app code
    live/discovery.yaml  what read-only discovery returns once access is granted
  truth.yaml             graph (nodes, edges, must_not, questions), plugins, requests
  truth_incidents.yaml   incident classes: members, labels, cause (generated)
gates.yaml               per-stage floors, forbidden ceilings, enforced_stages
generate_history.py      the seeded generator for servicenow/*.yaml and truth_incidents.yaml
```

**Every truth fact cites the sources it can be derived from** (`confluence:<page_id>`, `git:<repo>/<path>`, `cmdb:<sys_id>`, `live:<key>`), and the lab checks that each reference resolves. The truth never asks for something the sources don't contain. `must_not` lists what a careless reader would take from a stale, wrong or injected source. `questions` lists conflicts a good onboarding raises for review instead of resolving silently.

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

## Changing the history

Edit an incident class in `generate_history.py` and run it again. Never edit the generated YAML by hand: a test regenerates it and compares the files byte for byte.
