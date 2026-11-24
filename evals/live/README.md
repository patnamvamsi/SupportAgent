# Live-signal reference systems (ADR-0022 §5, ADR-0023)

The systems under `evals/onboarding` are ticket-driven: a ServiceNow history, Confluence and a CMDB. The systems here have none of that. Like most systems a small team runs, they have code, a Compose file, and the Prometheus and Loki that watch them. This is the alerts-only path the installer offers.

| | `system-d`: Meter readings |
|---|---|
| Stack | Python services under Docker Compose: a feed publishing to Kafka, a writer consuming into Postgres and keeping its cursor there, an HTTP API reading from it, and a backfill run by hand |
| Sources | one repository (13 files): `docker-compose.yml`, each service's code, Dockerfile and requirements |
| Signals | 7 days of container, log-volume and topic-offset history (`signals.yaml`), with daily quiet windows and one past outage |
| Incidents | a producer stall (the feed runs and logs but publishes nothing) and a writer outage |

## Layout

```
system-d/
  sources/git/   the checkout the installer points the instance at
  signals.yaml   what its Prometheus and Loki show; scenarios change it
  truth.yaml     the graph, catalog, detectors (with measured thresholds), what runs,
                 and what each incident must fire and where its case lands
```

`tests/unit/system_d.py` serves `signals.yaml` as the Prometheus and Loki HTTP APIs, so onboarding and the detector evaluator read it through the same adapter a deployment uses. `tests/unit/test_system_d.py` then plays the whole story with no model:
1. the installer's answers;
2. self-onboarding;
3. a person accepting each list with one click;
4. the two incidents, evaluated by the worker and triaged into cases.

Every incident found on the proving ground becomes a scenario here before the fix it motivates is accepted (ADR-0022 §5). The producer stall is the first: a stalled topic was missed by every log and container check.
