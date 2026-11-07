# Next steps (as of 2026-11-02)

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
- Oracle history from AWR/ASH (`DBA_HIST_*`): **not pursued**. There is no Diagnostics Pack licence (maintainer, 2026-11-02), so Oracle lock and session evidence stays point-in-time: those classes are proven live, in shadow (1.3).
- An MCP binding that maps a time argument.

### 1.2 Bind approved credentials and verify they are read-only (*delivered*, WP-O5b, ADR-0019)

`supportagent onboard bind <id>` takes an approved request's credential from a hidden prompt or `--stdin`, never from a web page.
- **The probes:** the contract's verifier runs them with the credential. `verify_can` must work. Every `verify_cannot` must be refused. The probes never write: they read the system's own account of the credential's privileges, or make an operation that changes nothing.
- **The verdict:** proven read-only, the credential goes to the vault (`onboarding.credentials_vault`, sealed under the master key, never the database) and the request is `bound`. Otherwise nothing is stored and the request reopens with why. Anything that can't be told counts against the credential.
- **The record:** every probe is on the request and in the audit trail, redacted of the credential.
- **Use:** a bound `oracle_ro` credential serves the Oracle tools.
- **Keeping it true:** `onboard verify` re-runs every bound credential's probes; one that can write now is unbound and reopened.
- Done: fakes of Oracle, MongoDB and RabbitMQ, each with a read-only and a read-write account, bind the first and refuse the second.

**Follow-ups:**
- Verifiers for the other contracts (GitHub, Snowflake, Kafka, Airflow, Grafana and the rest). Until one exists, a contract is never bound.
- Starting an MCP server (MongoDB, RabbitMQ) with its bound credential: the installer's job (§3).
- ~~Run `onboard verify` on a schedule~~ (*done*): the worker's `onboard.verify` job runs every `onboarding.verify_interval_h` (default 24; `0` turns it off). It logs a warning for each credential it unbinds, and its `rows` count those.
- Meeting real systems: the verifiers are tested against faithful fakes only.

### 1.3 Shadow replay (*delivered*, WP-O6c, ADR-0020)

Readiness runs the lab harness: the real classifier and RCA, with tools from evidence. Shadow replay runs the same incidents through the real pipeline in shadow mode, so readiness also covers dedup, grouping, calibration, the severity cap and routing.
- **The gate:** a class the lab proved stays ready only when shadow routes 75% of its incidents with its label, each on its own case. The lab's baseline guards the lab's own verdict.
- **The finding:** all three reference systems measure 0% ready. Calibration needs about 16 correct classifications per label, and they hold 3 to 7 incidents per class. The floor stays (maintainer's decision).
- **Learning live:** every resolved live case calibrates its label. Status says how many more each label needs.
- **Proven live:** in shadow, RCA also runs on held-back cases with bound credentials. Its result stands in for evidence an incident never had, once the ticket resolves.
- **Catalog gaps:** services the catalog wouldn't route are reported as a configuration need.

**Follow-ups:**
- ~~Have onboarding propose the catalog from the graph~~ (*done*): `supportagent onboard catalog` drafts `catalog.proposed.yaml` for review. The paging integration is left to a person.
- Prove `onboard ingest --refresh` plus `onboard run` on a real system, on a schedule.
- Alert storms in the replay: the histories hold tickets only.

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

- ~~Git catalogues~~ (*done*): `plugins.catalogs` accepts `{git, ref, path}`. The ref must be a full commit id. The commit is fetched shallowly and read-only into `plugins.cache_dir`, checked against the pin, and stripped of `.git`. `supportagent plugin fetch` fetches it, or onboarding does when it senses; `doctor` never fetches.
- ~~Live discovery declared by plugins~~ (*done*): `provides.discovery` maps a live capability's result onto the graph as data. The six core mappings moved into the kubernetes, oracle, kafka, snowflake and airflow plugins, and the three reference systems' structured facts are identical before and after.
- ~~Fix two flaky tests~~ (*done*, #102 and its follow-up). Both came from one bug: a CEL check that ran out of its 50 ms budget read as "didn't hold". That happened whenever another thread held the interpreter lock, such as the MCP server in the canary test. The canary's clean run then missed a cause, and SOP auto mode's positive check went missing. Now the budget is 1 s, and a timeout is a step that didn't run. `test_sop_auto_db` pins that a check that couldn't be evaluated never sends on a grant.
- ~~Knowledge for System B's stack~~ (*done*): the kafka, snowflake, spark, data-quality and streamlit plugins are evaluated, data only (declared capabilities served by MCP), with packs and labs (13 of 16 lab scenarios pass, no false causes) and System B's evidence. With a perfect classifier its causes hold 98% of the incident weight in the lab. With Qwen, six classes are lab-ready and, like A and C, held back in shadow by calibration. A failed job is the core `batch/failure` label (the plugins' duplicate `job/failure` is gone). Airflow is still a skeleton: none of System B's causes is an Airflow one.
- The installer:
  - ~~catalogues and onboarding~~ (*done*): the wizard's optional *Onboard this system* writes `local/system.yaml` and `plugins.catalogs`, and `./install.sh` runs `onboard run` as its last step (`./install.sh onboard` again).
  - Still open: start each MCP server a plugin needs with its bound credential (ADR-0019 §5). It waits for a real system to name the servers: the reference plugins' images are placeholders, and each server takes its credential its own way, which the plugin manifest would have to declare.

## Suggested order

1.1 and 1.2 (delivered), which together unblock a real system; then 2 (control and observability, delivered); then 1.3 (delivered); then 1.4 on a real system as soon as one is available. Section 3 fits around them.
