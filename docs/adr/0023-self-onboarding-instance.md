# 0023. Self-onboarding: install, connect, and the instance does the rest

- **Status:** accepted (2026-11-22, maintainer: "a new setup instance, given LLM access, should be able to build this knowledge graph by itself and host all of it in its instance … get from GitHub, set up config and LLM access, and it should do the rest by itself")
- **Date:** 2026-11-22
- **Code PR:** WP-221 to WP-226
- **Builds on:** ADR-0015 (onboarding), ADR-0016 (plugins as data), ADR-0017 (onboarding autopilot), ADR-0022 (SRE on live signals; the proving ground)

## Context

The first pass on the proving ground worked: alerts flow into a shadow instance, detectors are measured from the system's own history, and onboarding found the stack from its files. But a person (an engineer with the repository checked out) ran every step by hand from a laptop: copying files, running `onboard ingest`, `detect`, `graph`, `catalog` and `detectors` with placeholder environment variables, copying rule files to the monitoring host, writing the deployment's config and Keycloak client, and starting a separate instance. The instance the owner then opened had none of the onboarding results in it: no graph, no catalog review, and one test case. The owner's verdict on the UI was "very underwhelming".

The graph itself was shallow. It read 14 setup files (Compose files, Dockerfiles, dependency lists) and produced 14 services with 14 `depends_on` edges. It read no application code. So it couldn't say which service writes which Kafka topic or table, what runs on a schedule, which endpoints a service serves, or which external APIs it depends on: exactly what a person needed on 16 Nov to diagnose a feed that stopped without an error.

## Decision

### 1. The setup contract

**Setting up SupportAgent for a system means three things, and only these:**
1. **Get it:** clone from GitHub and run `./install.sh`, or run the published image.
2. **Configure it:** the LLM; who signs in; and the system's sources. Sources are its repositories (GitHub with a read-only token, or a local checkout mounted read-only), its signals (Prometheus and Loki URLs, read-only), and optionally its tickets and documents.
3. **Review what it proposes:** in the UI.

**Everything else is the instance's job:** reading the sources, building and hosting the system graph, drafting the catalog, measuring and proposing detectors, evaluating the enabled ones, and triaging what fires. Each step is resumable, budgeted and read-only. A step that needs a person (a credential, a review, a plugin) appears as a need on `/ui/onboarding`, never as a command for someone to run elsewhere.

The one exception is a system with **no monitoring at all**. The instance has read-only access, so it can't install exporters on another host. It offers the exporter bundle (`docker/observability`, with the targets it discovered filled in) as a download, and onboarding continues with whatever signals exist.

### 2. Onboarding runs inside the instance

- **The autopilot (ADR-0017) runs as a worker job** (`onboard.autopilot`, under a lease), on a schedule and on demand from `/ui/onboarding` ("Run now"). Its workspace lives on the instance's data volume. Results go to the instance's database, where the UI reads them.
- **The system manifest is stored by the instance and edited from the UI:** sources, budget, readiness gates. A file manifest still works for the CLI and for tests.
- **The autopilot's steps** are ingest, sense, graph, catalog, detectors, incidents (when there are tickets), requests, evidence, readiness and gaps. Steps that have nothing to work on (no ticket history) finish as `done: nothing to do`, not as `blocked`.

### 3. The system graph is built from code, configuration and live signals

The graph keeps the existing layers (ADR-0015 §3) and adds a **code layer**.
- **Static analysis, per language.** Python is first, through its AST: no imports are executed and nothing is run. Library knowledge is declared by plugins as **code rules**: "a call to `send` on a `KafkaProducer` produces to the topic in its first argument", "`__tablename__` names a table the module's models write", "an APScheduler `add_job` with a `CronTrigger` is a scheduled job". The analyser resolves module constants, `os.getenv` defaults and imports across a repository. An f-string becomes a pattern (`binance.ticks.*`). Every fact cites `repo:path:line`.
- **What the code layer finds:** topics produced and consumed; tables read and written (ORM models and SQL strings); scheduled jobs and tasks; HTTP routes served; outbound hosts; cache and pub/sub use.
- **Components own their code.** A Compose service's `build.context` (or a Kubernetes image's repository) ties a component to a repository and path, so the code facts in that tree belong to that component. Duplicates across files (`celery_worker` and `celery-worker`) are reconciled by general rules, and a component defined in a file but not running (no container in the live signals) is marked `defined, not running`.
- **Live corroboration.** A topic the code produces that the Kafka exporter sees advancing, or a table it writes whose insert rate the Postgres exporter reports, is marked **observed**. A fact found only in code stays **declared**. A live signal no code explains becomes a question for review.
- **The LLM summarises and resolves; it doesn't invent.** For each component it writes a short summary (purpose, inputs, outputs, schedule) from that component's code facts, with quotes. It may resolve a reference that static analysis left open (a topic held in a variable) as an `inferred` fact with its quote, which needs review. It never adds an edge without a citation. All of this runs within the manifest's token budget, and summaries are cached by content hash.
- **Review in the UI.** The graph view shows layers (runtime, data flow, storage, work, interfaces) and each component's page shows its facts with citations, its summary, its detectors and its cases. Confirmed facts feed the catalog, the detectors and RCA (WP-216).

### 4. Detectors are evaluated by the instance

The detectors a person enables are **evaluated by the instance itself**. It queries Prometheus and Loki on each detector's interval with the same read-only access onboarding used, and a firing detector becomes an `AlertEvent` in the same intake as a webhook alert. So enabling a detector needs no write access to the monitoring host. Rule files (`--apply`) remain available for teams that prefer their own Alertmanager. The two are mutually exclusive per detector, so nothing fires twice.

### 5. The installer asks about sources, not about ServiceNow

`./install.sh` asks for the LLM, sign-in, repositories, signals, and optionally tickets and documents. A deployment with no ticket source runs in shadow mode with alerts only (#163). Its answers file stays the non-interactive path for CI and for the acceptance run below.

### 6. Acceptance: nobody hand-holds the proving ground

Done means a **fresh instance on the proving ground's host, installed from GitHub with an answers file** (LLM, sign-in, the repositories mounted read-only, the Prometheus and Loki URLs), onboards the system with no other step from anyone. Within the budget, the instance shows:
- the code-layer graph with cited facts;
- the drafted catalog;
- measured detectors to enable;
- and, once they're enabled, cases from real firings.

Every step that needed a person is listed on `/ui/onboarding` as a need. The same run on reference System D is the CI test.

## Consequences

- The laptop runs of WP-213 to WP-215 become a regression baseline. From now on, the proving ground is set up only through the contract in §1.
- **A new migration** stores the manifest, the code-layer facts (with citations and observed/declared status) and component summaries.
- Plugins gain a `code:` section. The analyser lives in `adapters/` (it reads files), and the domain stays free of I/O.
- The LLM cost of onboarding becomes visible per step on `/ui/onboarding`. Summaries are cached, so re-runs pay only for changed code.
- Other languages (Java, TypeScript, Go) follow the same shape later: an analyser per language, with code rules from plugins.

## Work packages

| WP | Title |
|---|---|
| WP-221 | **Code layer:** the Python analyser, plugin `code:` rules (Kafka, SQLAlchemy/Django/SQL, APScheduler/Celery, FastAPI/Django routes, HTTP clients, Redis), component ↔ repository from build context, reconciliation, live corroboration |
| WP-222 | **Component summaries:** LLM summaries and inferred facts with quotes, a token budget, and a cache by content hash |
| WP-223 | **In-instance autopilot:** worker job, workspace on the volume, manifest in the database and edited in the UI, "Run now", progress and needs, catalog and detectors as steps |
| WP-224 | **Detectors evaluated by the instance:** an internal evaluator; a firing becomes an `AlertEvent`; enable and disable from the UI |
| WP-225 | **System view:** layered graph, component pages (facts, summary, detectors, cases), and links from cases into the graph |
| WP-226 | **Installer and acceptance:** sources-first installer, the alerts-only path, and the fresh-install acceptance run on the proving ground and on System D |
