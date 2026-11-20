# 0022. v0.2 re-scoped: SRE on live signals, proven on a real system that the product discovers

- **Status:** accepted (2026-11-20, maintainer: "rescope, but use Vritti as the proving ground; don't cater to Vritti as the core product. The core product should be able to discover Vritti and act accordingly"). §6 (acting on a system): the maintainer's go-ahead came on 2026-11-21 ("the agent would eventually carry out steps on approval"), so it is built as WP-218.
- **Date:** 2026-11-20
- **Code PR:** none yet
- **Supersedes:** the v0.2 phase plan in ROADMAP §3 and the WP-201 to WP-210 table (12-wp-v0.2.md), as described in §8
- **Builds on:** ADR-0011 (modular framework), ADR-0013 (connector seam), ADR-0014 (SOP-driven requests and auto mode), ADR-0015 to ADR-0020 (onboarding, plugins as data, evidence, credential binding, shadow replay), ADR-0021 (transaction tracing)

## Context

The roadmap's v0.2 ("support analyst with hands") assumed a design partner: a ServiceNow instance with six months of tickets, an Airflow estate to re-run jobs on, a service catalog to fulfil requests from, and a business willing to split its services into assisted and control sets. No partner has appeared. 0.1.0 shipped with every exit criterion that depends on one still open (BL-037, the exit demo).

Meanwhile a real system is available: the maintainer's trading platform (Vritti), a Docker Compose stack of Python services, Kafka, Redis and TimescaleDB, running in paper mode. Between 2026-11-08 and 2026-11-19 it had four real incidents, each diagnosed by hand:
1. **Dependencies weren't restarted after a reboot.** The database, broker and cache had no restart policy, and the apps came up without them.
2. **A process was alive, but its work had stopped.** A websocket feed went silent with no error logged. The container stayed "Up", and three days of market data went missing.
3. **A stale watermark threatened a replay.** Restarting a consumer whose cursor lagged would have replayed two days of bars into the order system.
4. **The host suspended itself**, which stopped all collection, 19 times in three weeks.

SupportAgent caught none of these, because each one raised no ticket and SupportAgent only hears about incidents through tickets. Yet every one followed the product's loop exactly: notice a symptom, gather evidence from logs, metrics and records, find the cause, and apply a known fix that needs a human's approval.

Two things follow. First, v0.2 should make SupportAgent an SRE that works from live signals as well as tickets. Second, a real system shouldn't wait for a partner, provided the work on it doesn't turn the core into that system's tool. The project already has that rule: onboard any system generically; system-specific material is only ever a regression fixture.

## Decision

### 1. The proving ground, and the line it can't cross

v0.2 is proven on Vritti. Vritti is a **proving ground, not a customer of the core**:
- **Nothing in `src/`, `packs/`, `plugins/`, `prompts/` or `taxonomy/` names it.** A test (`test_core_names_no_target_system`) fails if any name in `tests/data/proving_ground_names.txt` appears there. The list is tracked, so CI checks the real names; docs may name the proving ground.
- **Everything Vritti needs comes from generic mechanisms.** Onboarding discovers its stack. Plugins and packs cover the technologies it uses (Docker Compose, Redis, Celery, Kafka, Postgres/TimescaleDB, Python services), each written for that technology, with its own lab, for any system that uses it. Detectors, runbooks and flows are the deployment's reviewed data, written by onboarding or by a person.
- **Vritti's settings, credentials, recordings and findings stay under the deployment's own directory** (`local/`, git-ignored), like any deployment's.
- **Each Vritti incident becomes a generic lab scenario** on a synthetic reference system ("System D", a Compose streaming stack) before the fix it motivates is accepted. That system is the regression test, the same rule as Systems A to C.
- **A generic change needed only by Vritti still needs a second case.** It must work on System D or another reference system in CI.

### 2. Live signals are an intake, alongside tickets

Alerts become first-class incidents, flowing through the same dedup, storm grouping, triage, readiness and gate as tickets. To make that work:
- **Native receivers for the common alerting tools:** Prometheus Alertmanager and Grafana Alerting webhooks, authenticated with a bearer token or basic auth (what those tools can send). The HMAC-signed generic webhook (WP-113) remains for everything else. Mapping presets turn each payload into `AlertEvent`, with no JSONPath to write.
- **Firing and resolved:** a resolved notification closes the alert's evidence window, and a resolved alert with an open case is a lifecycle event, never a silent close.
- **Service resolution from labels:** the catalog resolves a service by its alert labels (`service`, `job`, `container`, `compose_service`, `namespace`), and onboarding writes those label mappings into the catalog it proposes.

### 3. Onboarding proposes the detectors

A system with no alerting, or with alerting that misses this week's failures, is the normal case. Onboarding reads the system graph and the signals available, then proposes **detectors**: reviewed rules in the deployment's alerting format (Prometheus and Loki rule files). Each one comes from a generic failure class:

| Detector class | From the graph | Signal |
|---|---|---|
| **Progress stopped** | a component that writes to a store at a steady rate | inserts per table, topic offsets or log lines per container stop changing, while the component reports up |
| **Liveness without progress** | a long-running process | the container or process is up, but its log silence exceeds its normal gap |
| **Dependency not up** | a service that depends on another | the dependency is down, or has no restart policy (a finding for review, not an alert) |
| **Watermark lag** | a consumer with a cursor, offset or watermark | lag beyond its normal bound, plus a warning that a restart would replay it |
| **Host interruption** | the host | reboots, suspends, clock jumps |

A detector is data: it is reviewed like a flow (ADR-0021), and a person enables it. Its normal rates and gaps are measured from the system's own history before it's proposed, never guessed. Onboarding never writes into the alerting system itself; it hands over files to review and load.

### 4. The signals: a generic exporter stack, opt-in

`docker/observability` gains an opt-in exporters file for any Compose system. It includes cAdvisor, node-exporter, postgres-exporter (with a read-only monitoring role), kafka-exporter, redis-exporter, blackbox-exporter and Alertmanager routed to SupportAgent. Targets come from settings, not code. The plugins for those technologies declare which metrics serve which capability, so onboarding can discover what a deployment measures.

### 5. Code-aware RCA

RCA follows an error signature into the code:
- **Locate:** a stack frame, exception or log message is located in the system's repositories, read-only at a pinned commit (the existing Git source).
- **Discover the mapping:** onboarding works out which repository and path build each running component, from Compose build contexts, image labels and the graph.
- **Find what changed:** recent commits to the lines involved.
- **Cite it:** the evidence is a cited code excerpt, so claims pass `entail` like any other.

A suggested fix may be attached to the case as a diff for a person to review and apply. Opening a pull request is a later action kind, under the gate.

### 6. Acting on a system (go-ahead 2026-11-21: on approval)

A resolved case's recovery steps (for example: stop the consumer, restart the feed, advance the watermark, start the consumer) become an SOP runbook (ADR-0014). Each step is an action kind with these properties:
- a **dry run** shown before approval;
- an **idempotency key**;
- **preconditions** checked when it runs;
- a **verification check** after it runs;
- a declared **revert**, or none.

Every step runs under a per-step human approval or a conditioned SOP grant, as today. High-risk kinds also need two approvers (the old WP-206).

Write access uses its own credential, bound only once its probes show it can do exactly the declared operations and nothing else. That extends ADR-0019 from "proven read-only" to "proven to be exactly this scope". The first action kinds are generic: restarting, stopping and starting a container or Compose service, and a reviewed single-row record update keyed by one id. The maintainer gave the go-ahead on 2026-11-21: the agent carries out a step once it is approved, or once a conditioned SOP grant a person approved covers it. Never on its own judgement.

### 7. Exit = v0.2 demo, on the proving ground

1. A SupportAgent with a fresh install runs `onboard run` against Vritti's host. It discovers the Compose stack, its stores, feeds and dependencies, and the exporters. It proposes detectors; the maintainer reviews and enables them.
2. Reproducing failure 2 (a feed that goes silent while its process stays up) raises an alert within the detector's window. Triage classifies it, and RCA cites the stalled store, the log silence and the code path that has no reconnect.
3. SupportAgent proposes the recovery runbook with its dry run. A person approves it, the agent carries it out step by step (the proving ground stays in paper mode), and SupportAgent verifies that progress resumes.
4. The outcome calibrates the label (ADR-0020). The same scenario passes on System D in CI, and `test_core_names_no_target_system` passes.

### 8. What happens to the old v0.2 work packages

| Old | Now |
|---|---|
| WP-201 job re-run | becomes action kinds under §6. The Airflow and GitHub Actions re-runs stay on the list for when a system needs them |
| WP-202 service requests | **deferred** until a ticket-driven deployment needs them |
| WP-203 deep RCA and correlation | kept, together with code-aware RCA (§5) |
| WP-204 MCP | delivered as WP-M06. stdio servers stay BL-049 |
| WP-205 runbook distillation | kept. It distills from resolved live cases (§6), with the holdout disjoint in time and signature as before |
| WP-206 two-person rule | kept, as part of §6 |
| WP-207 outbound DLP | kept |
| WP-208 PITR in CI | kept |
| WP-209 assisted treatment/control split | **moves to v0.3**, still waiting on a partner (C22 value story) |
| WP-210 release | kept, with §7's exit demo |

## Consequences

- **Better:** v0.2 is no longer blocked on a partner. Real incidents with real outcomes start calibrating labels as soon as detectors fire. The SRE scope matches what the product has grown into. Each generic piece (alert receivers, detectors, Compose and Redis plugins, code-aware RCA) serves any system.
- **Worse:** one proving ground can bias the design. The name guard, the System D scenarios and the "a second case for every generic change" rule exist to limit that. The ticket-driven, partner-shaped capabilities (service requests, the assisted value story) slip.
- **New tests:** `test_core_names_no_target_system`, `test_alertmanager_payload_maps_to_alert_event`, `test_grafana_alert_payload_maps_to_alert_event`, `test_resolved_alert_never_closes_case_silently`, `test_detector_rates_measured_not_guessed`, `test_detector_needs_review_before_enable`, `test_code_frame_located_at_pinned_commit`, `test_runbook_step_dry_run_before_approval`, `test_write_credential_bound_only_for_exact_scope`.
- **To watch:** detector noise (false-alert rate per detector, reported on `/ui/stats`), and how often code-aware claims are dropped by `entail`.
