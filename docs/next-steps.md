# Next steps (as of 2026-11-01)

Where SupportAgent stands after ADR-0015 (onboarding and plugins), ADR-0016 (plugins are data) and ADR-0017 (the onboarding autopilot), and what comes next, in order.

## Where it stands

`supportagent onboard run --system system.yaml --workspace <dir>` takes any system through one loop:
1. reads it, read-only;
2. senses which plugins apply, from those installed and any catalogue;
3. installs what's available, each proved on its own lab here;
4. maps the system and its incident history;
5. asks for least-privilege, read-only access;
6. replays the history per incident class against the 70% bar;
7. fits the classifier's calibration;
8. names what nothing covers.

It ends with what's needed from a person, ranked by the incident weight each item holds back. Re-running continues where it stopped.

Three reference systems (Java/Oracle, data platform, Node/MongoDB/RabbitMQ) are its regression tests. They are synthetic. **No real system has been onboarded yet.**

## 1. The path to a real system (in order)

### 1.1 Capture evidence from live tools (*delivered*, WP-E1, ADR-0018)

`onboard evidence` (and an `evidence` step in `onboard run`, before readiness) reads each past incident of a testable class as of its open time. It uses the probes RCA would plan, through the tool registry, so every read is a `tool_call` event.
- **Can read the past:** `logs.search` (Loki), `metrics.query` (Prometheus), `changes.recent` and `gha.recent_runs`. Each has `run_as_of`.
- **Point-in-time** (locks, sessions, pods, MCP-served capabilities): marked *not replayable*.
- **Aged out:**
  - older than `onboarding.retention_days` (logs 30, metrics 15, changes 90, CI 400 by default);
  - or the store's newest page doesn't reach back.

  Reported, never read as "nothing was wrong".
- **The replay rule:** an incident is replayed only when every read of its cause came back. Without the decisive evidence, the replay drifted to another hypothesis: a lock read only as the pool exhaustion it caused, six false causes in System A's lock class.
- A class with nothing replayable says why: *visible only live: prove it in shadow* (1.3), or *aged out*. It asks for evidence, not for a plugin.
- Done: fakes with history reach System A's fixture readiness exactly.

**Follow-ups:**
- Oracle history from AWR/ASH (`DBA_HIST_*`) needs the Diagnostics Pack licence, which is the partner's decision.
- An MCP binding that maps a time argument.

### 1.2 Bind approved credentials and verify they are read-only (WP-O5b)

Approving an access request records intent only.
- **What:** approving takes the credential (into the secret store, never the database), binds it to the plugin's first-party tool or MCP server, and runs the contract's probes:
  - `verify_can` must work;
  - `verify_cannot` must be refused: no insert, no update, no DDL, no publish, no delete.

  A credential that can write is refused and the request reopens with why. Every probe and its result is recorded.
- **Done when:** a fake Oracle, MongoDB and RabbitMQ, each with a read-only and a read-write account, bind the first and refuse the second.

### 1.3 Shadow replay (WP-O6c)

Readiness runs the lab harness: the real classifier and RCA, with tools from evidence. Shadow replay runs the same incidents through the real pipeline in shadow mode, so readiness also covers ingest, grouping, routing and the gates. It also proves classes whose evidence is only visible live.

### 1.4 A first real system (waiting on the maintainer)

Point `onboard run` at a real ServiceNow scope, Confluence spaces and repositories (see `docs/AWAITING-INPUT.md`). Expect:
- gaps the reference systems never showed;
- recordings to make with the real model;
- probably a plugin or two to write from `plugin new` scaffolds.

That run, not the fixtures, decides what comes after.

## 2. Operating what was added: control, monitoring, observability (WP-A6)

The onboarding work ships its outputs: the status page, readiness, gaps and drift. It ships almost none of the operational instrumentation the rest of the product has. Nothing it adds emits events, `doctor` checks none of it, and its policies have no switches. Before a real system relies on it:

### 2.1 Observe: events and an audit trail (*delivered*, WP-A6a)

Delivered as one new event kind, `onboarding`, whose `payload.action` names the event (the log goes from 20 kinds to 21, migration 0020). Access and graph decisions are written in the same transaction as the decision. The onboarding page shows the recent activity. `drift_found` and `plugin_disabled` land with the switches (2.3).


Add event kinds, written to the append-only event log like every other decision (INV-03), and shown on the case or onboarding timeline:

| Event | Why |
|---|---|
| `onboarding_step` (step, state, digest, detail) | what each run did, and when it was blocked |
| `plugin_installed` (id, version, catalogue, sha256, lab result, who or `auto`) | supply chain: who brought which code-free plugin in, from where, and how it did here |
| `access_decided` (request, decision, who) | access decisions today live only on the request row |
| `graph_fact_decided` (fact, decision, who) | the same for the SME review |
| `readiness_assessed` (ready share, per-class status, model) | how readiness moved over time |
| `calibration_fitted` (labels, n, model, in use or ignored) | which table triage scores against |
| `drift_found` (findings) | changes waiting to be re-onboarded |

### 2.2 Check: `doctor` (*delivered*, WP-A6c)

A new `onboarding` check:
- **Plugins:** it fails if the plugin index doesn't load, a pin doesn't verify (`tree_sha256` included) or an embedded pack is missing. It names the disabled plugins and warns on a catalogue that doesn't load.
- **Declared capabilities with no backend:** a capability a plugin declares, which a pack some service selects checks, with no `tools.bindings` entry is named. Otherwise the check silently never runs (for example `rabbitmq.queues` for a service whose stack is `rabbitmq`). Binding is a deployment fact, so this lives in `doctor`, not the offline gaps step.
- **Readiness freshness:** when the report was made, and a warning when the plugins changed since. The report now stores the index digest. Model and history changes still show on the onboarding page, not here.
- **Calibration in use:** whether triage uses the fitted table or the configured artifact, and why:
  - the switch;
  - nothing fitted yet;
  - edited counts;
  - the fitted table's model differs from triage's.
- **Routing:** `enforce` or `report`.

### 2.3 Control: switches (*delivered*, WP-A6b)

Delivered:
- `onboarding.routing` and `onboarding.calibration`;
- `plugin disable` / `enable`, kept in the index and audited;
- `onboard watch --every-hours N` (`--once` for cron) instead of a worker job. A cycle calls the model and can take minutes, so it runs as its own process and never stalls triage. Drift findings are audited (`drift_found`) and shown on the onboarding page.


| Setting | Default | What it does |
|---|---|---|
| `onboarding.routing` | `enforce` | `report` keeps measuring readiness but stops it gating triage and claims, for an incident or a first week |
| `onboarding.calibration` | `fitted` | `artifact` ignores the fitted table |
| `plugins.auto_install` | `none` | exists; documented as the supply-chain switch |
| `plugin disable <id>` / `enable` | | takes a plugin out of detection and packs without uninstalling it; recorded as an event |
| A scheduled `onboard run` | off | a worker job (daily, configurable) so drift is found without a person, raising a need when it finds something |

### 2.4 Monitor: `/ui/stats` and the onboarding page (*delivered*, WP-A6c)

- `/ui/stats` has a *Readiness and calibration* section:
  - RCA claims tagged `not_ready` (supported by evidence, held back by readiness), over cases with an RCA;
  - cases routed safe as `class-not-ready`;
  - the calibration in use, and why.
- The onboarding page shows the ready share over the recent assessments (from the audit trail), above the activity table.
- There is no Prometheus endpoint in the product today (it observes others, not itself). If the deployment wants one, the counters above are the first to expose.

## 3. Smaller follow-ups (any order)

- Git catalogues: a URL and a pinned ref, fetched read-only into a cache.
- Live discovery declared by plugins: mapping a capability's result onto the graph is still per capability in core.
- Fix two flaky tests:
  - the MCP canary on Postgres;
  - `test_sop_auto_db`'s daily cap on SQLite, which is possibly date-sensitive.
- Knowledge for System B's stack (Kafka, Snowflake, Spark, Airflow, data quality), done the ADR-0016 way: plugins as data, packs with labs.
- The installer: `./install.sh` should offer `plugins.catalogs` and `onboard run` as its last step.

## Suggested order

1.1 (delivered) then 1.2, which together unblock a real system; then 2 (control and observability, delivered); then 1.4 on a real system as soon as one is available; then 1.3. Section 3 fits around them.
