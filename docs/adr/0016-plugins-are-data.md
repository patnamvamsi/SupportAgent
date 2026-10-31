# 0016. Plugins are data: declared capabilities and declarative extractors, so a new stack needs no core code

- **Status:** accepted (2026-10-31; the maintainer asked for it: "yes please")
- **Date:** 2026-10-31
- **Code PR:** this ADR with its core changes (WP-P1); the proof, System C, in a data-only PR (WP-P2)
- **Builds on:** ADR-0015 §2 (one plugin model), ADR-0011 §2 (capabilities, MCP bindings)

## Context

The maintainer asked: when a System C arrives, is getting the right plugin enough, or does the framework need code changes? After ADR-0015, the answer was *mostly not enough*:

| Part | Before | Why it needed code |
|---|---|---|
| Detection, labels, causes, hypotheses, probes, runbooks, lab scenarios, access contracts, target finders | data | none |
| A new **capability** (`mongodb.slow_ops`) | code | its result schema came only from a built-in Python tool; an MCP binding could only serve a capability the core already knew, and the manifest's `schema_path` was never read |
| Reading a new stack's **config** into the graph (Node.js `package.json`, `config/*.json`, `.env`) | code | the extractors were a fixed list in `adapters/files/graph_extract.py`; a manifest's `extractors:` were only descriptions |
| A new **CMDB class** (a MongoDB CI) | code | a fixed class table, where an Oracle instance was the only database |
| **Target names** for runtimes with an area (`runtime:kafka/<cluster>`) | code | a fixed list of areas in `gap_plan.py` |

WP-O6a and WP-O8 each needed core edits for Oracle and Spring Kafka. Every new stack would too.

## Decision

A plugin declares, and the core runs, these three things. A new stack's plugin is YAML and Markdown. Where evidence needs a live system, an MCP server (out of process, ADR-0011) serves it.

### 1. Declared capabilities

```yaml
provides:
  capabilities:
    mongodb.slow_ops:
      description: Operations slower than the profiler threshold
      result_schema:            # an object schema; its required keys must be properties
        type: object
        required: [database, ops, max_ms]
        properties: {database: {type: string}, ops: {type: array}, max_ms: {type: number}}
      literal_args: []          # argument keys passed literally (a reviewed query)
  backends:
    - {capability: "mongodb.*", mcp: {image: ghcr.io/acme/mongo-mcp:1.0, tools: [slow_ops]}}
```

- **The catalogue** is the built-in tools' capabilities plus each one a plugin declares a schema for. Runbook and probe checks compile against it, lab worlds and readiness evidence are validated against it, and an MCP binding may serve any capability in it.
- **The generic `passthrough` profile** sends the bound arguments. It requires the reply to be the capability's JSON object, and the tool registry rejects any result off the declared schema.
- **The schema lives in the manifest,** so the plugin index's sha256 pins it like everything else.
- **A plugin can't redefine a built-in capability's schema,** and two plugins that declare one capability must agree on it.

### 2. Declarative extractors

```yaml
provides:
  extractors:
    - match: "**/package.json"
      format: json              # json | yaml | env | properties
      rules:
        - {key: "^name$", own: true, node: {id: "service:{name}", kind: Service, tech: [nodejs]}}
        - {key: "^dependencies\\.express$", value: ".", tech: [express]}
    - match: "**/config/*.json"
      format: json
      rules:
        - key: "mongo\\w*\\.uri$"
          value: "mongodb(?:\\+srv)?://[^/]+/(?P<name>[A-Za-z0-9_-]+)"
          node: {id: "database:{name}", kind: Database, tech: [mongodb]}
          edge: uses
        - {key: "url$", value: "^https?://(?P<name>[a-z0-9-]+)\\.[a-z0-9-]+\\.svc", edge: calls, to: "service:{name}"}
    - match: "cmdb:u_cmdb_ci_mongodb"     # installed CIs of the class become this node
      node: {id: "database:{name}", kind: Database, tech: [mongodb]}
```

- **A rule** matches a config file's flattened keys (a regex) and a string value (a regex whose named groups fill the templates, lowercased). From the repository's component it yields one of:
  - the component itself (`own`);
  - a node it relates to (`node` and `edge`);
  - a relationship to a node found elsewhere (`to` and `edge`);
  - technology the component has (`tech`).
- **Relationships** come from the graph's vocabulary only (`calls`, `uses`, `reads`, `writes`, `consumes`, `publishes`, `runs_on`, `depends_on`). Node kinds are the graph's eight kinds.
- **The built-in extractors run first.** A declared component rule claims only a repository nothing else claimed. Declared facts are layer 1 (config) like the built-in ones, so the graph's merge rules, review and drift treat them the same.
- **`extract: <name>`** still names a built-in extractor; it documents what core code reads.

### 3. No stack lists in the core

A runtime id's first segment is an area, and is dropped from a target's name, when it names the node's own technology (`runtime:emr/emr-batch` has tech `emr`). The list of areas is gone. `runtime:prod-eu1/payments`, a Kubernetes namespace, keeps its cluster.

## What still needs code

- **A new ticket system or docs source** (Jira, SharePoint) is a connector (ADR-0013) or knowledge source: code by design, because it carries the write path and redaction.
- **A config format no rule can express** (code, not config: Python's `SparkSession` options, Snowflake SQL) keeps a built-in extractor, or comes as a first-party, signed plugin package (ADR-0015 §2).
- **Live discovery's mapping of capability results to graph facts** (`kafka.topics` to topics) is still per capability in the core. A declared mapping is a follow-up. Until then a new stack's live layer is what Kubernetes and the CMDB show.
- **A first-party in-process backend** (like `oracle.*`) is optional: an MCP server can serve any declared capability.

## Proof

**WP-P2: System C**, a Node.js (Express) service mesh on MongoDB and RabbitMQ, on Kubernetes, with ServiceNow and Confluence. It is a reference system with its own truth, plugins `nodejs`, `mongodb` and `rabbitmq`, packs evaluated on their own labs, and readiness evidence. It lands in a PR that changes nothing under `src/`, and the onboarding lab enforces every stage on it.

## Consequences

- **Review moves to data.** A plugin's rules and schemas are reviewed in its manifest and pinned by the index. A bad regex, an unknown relationship, a template group the regex lacks, or a schema whose required keys aren't properties doesn't load.
- **The core keeps the trust boundary.** Capabilities stay T0-read, MCP servers are registered and bound by the deployment, every result is validated against its schema, and rules can only assert the graph's vocabulary.
- **The catalogue is read once per process** (the pinned registry); a newly pinned plugin applies at the next start.
