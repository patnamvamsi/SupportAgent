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

### 1.1 Capture evidence from live tools (WP-E1). The blocker

Readiness replays each past incident from the evidence captured at the time. The reference systems carry that evidence as fixtures; a real workspace has none. So today a real system ends every `onboard run` with *"not ready: no evidence captured"*, whatever its plugins.

- **What:** for each past incident in a testable class, call the plugins' read-only tools for that incident's time window, the same probes its runbooks use. That means logs and metrics around the open time, rollouts before it, and database state where the tool can read history. Store the results as `sources/evidence/<incident>.yaml`; readiness then replays from them.
- **Honesty rules:**
  - Evidence that has aged out of retention (Loki or Prometheus) is reported as missing, never guessed.
  - Point-in-time tools (current locks, current queue depth) can't see the past, so they are marked *not replayable*.
  - A class whose cause can only be seen live is reported as such: it can still be proven later in shadow (1.3).
- **Needs:** approved access (1.2) and a model.
- **Done when:** a workspace whose tools are fakes with history onboards to the same readiness as its fixture.

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

### 2.2 Check: `doctor`

- **Plugins:** the index loads; every pin verifies, `tree_sha256` included; each catalogue loads against its own index; embedded packs load.
- **Declared capabilities with no backend.** Today a capability a plugin declares counts as known, so a pack check on it looks available even when no MCP server is bound to serve it, and it silently never runs. `doctor` must name each one, and the gaps step should report it.
- **Readiness freshness:** the report's age, whether the plugins, model or history changed since it was made, and drift findings not yet accepted.
- **Calibration in use:** whether triage scores with the fitted table or the configured artifact, and why (model mismatch, edited counts).

### 2.3 Control: switches

| Setting | Default | What it does |
|---|---|---|
| `onboarding.routing` | `enforce` | `report` keeps measuring readiness but stops it gating triage and claims, for an incident or a first week |
| `onboarding.calibration` | `fitted` | `artifact` ignores the fitted table |
| `plugins.auto_install` | `none` | exists; documented as the supply-chain switch |
| `plugin disable <id>` / `enable` | | takes a plugin out of detection and packs without uninstalling it; recorded as an event |
| A scheduled `onboard run` | off | a worker job (daily, configurable) so drift is found without a person, raising a need when it finds something |

### 2.4 Monitor: `/ui/stats` and the onboarding page

- `/ui/stats` already counts safe-defaults by reason, so `class-not-ready` appears there with no change. Add the claims tagged `not_ready` (supported by evidence, held back by readiness), and the fitted-against-artifact calibration in use.
- The onboarding page shows the last run. Add a short history: the ready share over time, and installs and decisions from the events above.
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

1.1 then 1.2, which together unblock a real system; then 2 (control and observability); then 1.4 on a real system as soon as one is available; then 1.3. Section 3 fits around them.
