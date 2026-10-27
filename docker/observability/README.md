# Observability stack (WP-M06)

Prometheus, Grafana and Grafana's MCP server (`mcp-grafana`), for the tool binding work in ADR-0011 §2. The python-postgres pack's `metrics.query` capability can be served by the first-party Prometheus adapter or, through a binding, by `mcp-grafana`'s `query_prometheus`. Both read the same Prometheus, so the contract suite can compare them.

| Service | Port | Notes |
|---|---|---|
| Prometheus | 9390 | scrapes itself (`service="demo-api"`) and Grafana (`service="demo-grafana"`) |
| Grafana | 3300 | one provisioned datasource, uid `prometheus`; anonymous access is **Viewer** only |
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

## Contract check

```sh
SUPPORTAGENT_MCP_GRAFANA_URL=http://<host>:8810/mcp \
SUPPORTAGENT_MCP_GRAFANA_TOKEN=... \
SUPPORTAGENT_PROMETHEUS_URL=http://<host>:9390 \
uv run pytest -m contract tests/contract/test_mcp_grafana_live.py
```
