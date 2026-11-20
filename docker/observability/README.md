# Observability stack (WP-M06, BL-050)

Prometheus, Loki, Grafana and Grafana's MCP server (`mcp-grafana`), for the tool binding work in ADR-0011 §2. Two capabilities can be served either way, so the contract suite can compare the backends:

- `metrics.query`: the first-party Prometheus adapter, or `mcp-grafana`'s `query_prometheus`;
- `logs.search`: the first-party Loki adapter, or `mcp-grafana`'s `query_loki_logs`.

| Service | Port | Notes |
|---|---|---|
| Prometheus | 9390 | scrapes itself (`service="demo-api"`) and Grafana (`service="demo-grafana"`) |
| Loki | 3110 | single process, filesystem storage, 7-day retention |
| Alloy | (none) | ships `./demo-logs/<service>.log` to Loki with the label `service="<service>"`; optionally a Compose project's container logs (below) |
| Grafana | 3300 | provisioned datasources, uids `prometheus` and `loki`; anonymous access is **Viewer** only |
| mcp-grafana | 8810, path `/mcp` | streamable HTTP; callers need `Authorization: Bearer $MCP_GRAFANA_SERVER_TOKEN`; started with `-disable-write` |

The MCP server reaches Grafana as an anonymous Viewer, so it can read but can't write, whatever tool a caller asks for. SupportAgent also calls only the tools declared for the server.

## Run

```sh
umask 077
printf 'GRAFANA_ADMIN_PASSWORD=%s\nMCP_GRAFANA_SERVER_TOKEN=%s\n' \
  "$(openssl rand -hex 16)" "$(openssl rand -hex 24)" > .env
docker compose up -d
```

## Bind `metrics.query` to it

```yaml
tools:
  mcp_servers:
    grafana:
      url: http://<host>:8810/mcp
      token_env: MCP_GRAFANA_SERVER_TOKEN
      tools: [query_prometheus]         # the only tools SupportAgent will call
  bindings:
    metrics.query:
      mcp: grafana
      tool: query_prometheus
      profile: grafana.query_prometheus
      args: {datasourceUid: prometheus}
```

`supportagent doctor` then shows `mcp: grafana serves metrics.query`.

## Bind `logs.search` to it

Declare `query_loki_logs` on the server too (`tools: [query_prometheus, query_loki_logs]`), then add the binding:

```yaml
  bindings:
    logs.search:
      mcp: grafana
      tool: query_loki_logs
      profile: grafana.query_loki_logs
      args: {datasourceUid: loki}
```

Or read Loki directly: `tools: {loki: {base_url: "http://<host>:3110"}}`. To feed the demo, append lines to `demo-logs/<service>.log` on the host.

## Ship a Compose project's container logs (opt-in)

To trace a real system that runs under Docker Compose and logs to stdout, add the override:

```sh
DOCKER_LOGS_PROJECT=<compose project> \
  docker compose -f compose.yaml -f compose.docker-logs.yaml up -d alloy
```

Alloy then also reads that project's containers through the Docker socket. Each line is labelled `project="<project>"` and `service="<compose service>"`, so a flow stage can query `{project="p", service="worker"} |= "$object.id"`. Containers of other projects are dropped. The socket is mounted read-only, but the Docker API behind it isn't, so use this only on a host where giving Alloy that access is acceptable. On first start, Loki rejects lines older than its retention ("entry too far behind"); the errors stop once Alloy catches up.

## Exporters for a Compose-run system (opt-in, WP-212)

`compose.exporters.yaml` adds the signals SupportAgent's onboarding discovers and its detectors read (ADR-0022 §3, §4), attached to the system from outside: no change to its code. Each exporter has a profile, so run only what the system's stack has:

| Profile | Exporter | Signals |
|---|---|---|
| `containers` | cAdvisor | each container's state, restarts, CPU and memory, with its Compose labels (privileged: read-only views of the container runtime) |
| `host` | node-exporter | reboots, uptime, disk, load |
| `postgres` | postgres-exporter | per-table inserts and updates (`pg_stat_user_tables`), connections, locks; with a `pg_monitor` read-only role |
| `kafka` | kafka-exporter | topic offsets, consumer group lag |
| `redis` | redis-exporter | up, memory, clients, keyspace |
| `http` | blackbox-exporter | HTTP probes of the URLs in `BLACKBOX_TARGETS` |
| `alerts` | Alertmanager | receives the detectors' alerts; sends nowhere until you add SupportAgent's receiver |

Targets come from `.env`:

```sh
EXPORTERS_TARGET_NETWORK=<the system's compose network>   # kafka, redis, postgres, http
PG_EXPORTER_DSN_HOST=host.docker.internal                 # or the database's service name
PG_EXPORTER_DSN_PORT=5432
PG_EXPORTER_USER=<monitoring role>
PG_EXPORTER_PASSWORD_FILE=/path/to/password               # a file, never the value
KAFKA_EXPORTER_SERVER=kafka:9092
REDIS_EXPORTER_ADDR=redis://redis:6379
BLACKBOX_TARGETS=/path/to/blackbox-targets.yml            # from exporters/blackbox-targets.example.yml
DETECTOR_RULES_DIR=/path/to/rules                         # reviewed detector rules (WP-215)
COMPOSE_PROFILES=containers,host,postgres,kafka,redis,http,alerts
```

```sh
docker compose -f compose.yaml -f compose.exporters.yaml up -d
```

Prometheus finds the exporters by Docker DNS (`exporters/scrape.yml`): one that isn't running adds no target, so there is no false "down" series for a detector to read. No exporter publishes a host port. Prometheus keeps 30 days (`PROMETHEUS_RETENTION`), enough for onboarding to measure a store's normal rates before it proposes a detector.

## Contract check

```sh
SUPPORTAGENT_MCP_GRAFANA_URL=http://<host>:8810/mcp \
SUPPORTAGENT_MCP_GRAFANA_TOKEN=... \
SUPPORTAGENT_PROMETHEUS_URL=http://<host>:9390 \
SUPPORTAGENT_LOKI_URL=http://<host>:3110 \
uv run pytest -m contract tests/contract/test_mcp_grafana_live.py
```
