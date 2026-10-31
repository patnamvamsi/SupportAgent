# Plugins (ADR-0015 §2)

One model for everything pluggable. A plugin is `plugins/<id>/plugin.yaml` and can provide:

| Part | What it is |
|---|---|
| `provides.pack` | A skill pack by id under `packs/`: taxonomy, hypotheses, probes, runbooks, prompt fragments, lab scenarios (ADR-0011 §1) |
| `provides.capabilities` | Read-only evidence, such as `oracle.locks` or `kafka.consumer_lag`, each with one result schema. A built-in tool's schema is its own; for a new capability, the plugin declares `result_schema` (and `literal_args`) and an MCP server serves it, with no core code (ADR-0016) |
| `provides.backends` | How a capability is served: a `first_party` adapter (ours, signed, in process) or an `mcp` server (out of process) |
| `provides.extractors` | What onboarding reads off a system with this plugin (WP-O3). `extract: <name>` names a built-in extractor. A declarative one (ADR-0016) has a file glob, a `format` (`json`, `yaml`, `env`, `properties`) and `rules`: a flattened key and a value regex whose named groups fill a node (`own` for the repo's component), a relationship (`edge`, `to`) or `tech`. `cmdb:<class>` with a `node` turns installed CIs of a class into nodes |
| `provides.labels`, `provides.causes` | The problem labels and known causes (`oracle/lock_contention`) the incident map can use when the plugin applies (WP-O4) |
| `requires.<contract>.find` | Where onboarding finds the contract's targets, one request each (WP-O5): `graph` nodes of a kind and technology, the manifest's `repos` on a host, installed `cmdb_class` items (a `name` regex group names the target), or a `docs` regex group |
| `provides.needs` | Capabilities this plugin's knowledge uses that other plugins provide |
| `detectors` | The signals that say a system needs it: files (with a regex), incident text, current docs, CMDB classes, where the code lives, change-caused incidents. Fired weights add up to `min_score` |
| `requires` | One least-privilege access contract per backend. The grant text is quoted in an onboarding request, and the `verify_can` / `verify_cannot` probes run when the access is provisioned (WP-O5) |

**What a plugin can never do** is checked at load:
- Every plugin is `T0-read`.
- A grant that writes (`INSERT`, `UPDATE`, `ALTER`, `GRANT DBA`, `ACCOUNTADMIN`, `OPERATE`, `cluster-admin` and so on) doesn't load.
- A `third-party` plugin can ship MCP servers and data, never in-process code.
- A backend can only serve a capability the plugin declares.

**Pinned.** `plugins/index.yaml` records each plugin's version and the sha256 of its manifest. A manifest that changes without being re-pinned doesn't load, and neither does a plugin directory the index doesn't list. After reviewing a change, run `supportagent plugin index`. Signatures come with external catalogs.

**Status.** `skeleton` means the plugin's contracts and detectors exist, so it can be detected and requested, but its knowledge hasn't been evaluated. Its incident classes can't leave shadow mode. A plugin becomes `evaluated` once its pack passes its lab gates. `oracle`, `java-spring`, `kubernetes` (WP-O6a) and `python-postgres` are evaluated.

**Causes are tested by hypotheses.** A cause `oracle/lock_contention` is tested by the pack hypothesis `oracle_lock_contention` (the `/` becomes `_`); a built-in cause such as `recent_change_push` is a common hypothesis already. An evaluated plugin has a hypothesis, with a probe, for every cause it declares (`tests/unit/test_plugin_packs.py`).

**Backends.** `oracle.*` is first-party (`adapters/tools/oracle.py`): fixed `SELECT`s over the views `oracle_ro` grants, each in a read-only transaction that is rolled back. A deployment configures `tools.oracle.databases.<name>.dsn_env` and installs `python-oracledb` itself; it is not a core dependency. `k8s.pod_status` and `changes.recent` were first-party already.

```bash
supportagent plugin list
supportagent plugin show oracle
supportagent plugin validate
supportagent onboard detect evals/onboarding/system-a     # which plugins a system needs, and why
supportagent eval onboarding                              # the lab; the plugins stage is enforced
```

| Plugin | For | Access it asks for |
|---|---|---|
| java-spring (evaluated) | Spring Boot services: JVM, pools, Spring errors | none: uses logs and metrics |
| oracle (evaluated) | locks, tablespaces, ORA- errors, plans | `oracle_ro`: `SELECT` on the `V$`/`DBA_HIST` views |
| angular | stale bundles, CORS, UI-side API errors | none |
| kubernetes (evaluated) | pods, restarts, OOMKills, rollouts | `k8s_ro`: get/list/watch in one namespace |
| grafana | logs (Loki) and metrics (Prometheus) through mcp-grafana | `grafana_ro`: Viewer |
| github | deploys and merges as evidence | `github_ro`: a read-only fine-grained token |
| kafka | consumer lag, topics, Kafka Connect | `kafka_ro`: DESCRIBE ACLs |
| snowflake | queuing, tasks, credits | `snowflake_ro`: `ACCOUNT_USAGE`, `USAGE` and `MONITOR` |
| spark | executor OOM, skew, streaming progress | `spark_history_ro`: the History Server, read-only |
| streamlit | app crashes and memory | none |
| airflow | DAG and task failures | `airflow_ro`: Viewer |
| data-quality | freshness, volume, schema drift along lineage | none |
| python-postgres | the evaluated python-postgres pack | none |
