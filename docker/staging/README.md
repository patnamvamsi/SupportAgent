# Staging on the LAN host

Runbook step 3 (06 §6 exit demo, BL-023): SupportAgent in shadow mode on
192.168.0.201, against the developer instance `dev393479`, with Keycloak as a
real OIDC provider and Qwen on LM Studio as the classify model.

| Service | URL | Notes |
|---|---|---|
| SupportAgent | http://192.168.0.201:8800/ui | API, admin UI and worker in one container |
| Keycloak | http://192.168.0.201:8880 | realm `supportagent`; users `ada` (admin on first login) and `bob` |
| Postgres 16 | internal | volume `pgdata` |

## First start

Copy the repo to `~/supportagent-staging` on the host (no `.env` files), then:

```bash
cd ~/supportagent-staging/docker/staging
cp staging.env.example .env && chmod 600 .env    # fill it in; single-quote special values
docker compose --env-file .env up -d postgres keycloak
./keycloak-setup.sh                              # realm, client, users; writes OIDC_CLIENT_SECRET
docker compose --env-file .env run --rm app migrate --config docker/staging/supportagent.yaml
docker compose --env-file .env run --rm app doctor --config docker/staging/supportagent.yaml
docker compose --env-file .env up -d app
```

Sign in at `/ui/login` as `ada` and approve the default T1 grants at `/ui/setup`.
Sign `bob` in from a private window, because logout doesn't end the Keycloak
session yet (BL-046). Then make him an approver at `/ui/users`.

## Notes

- Sync starts at "now minus `overlap_s`", so existing incidents are not
  replayed. Create or update an incident as a person: records the integration
  user writes are ignored as the agent's own.
- The catalog lists the developer instance's CIs by name (`resources`) and by
  sys_id (`cmdb_ci`). An incident without a CI safe-defaults as
  `unresolved-service` before any LLM call (ARCH §4).
- Docker Desktop leaves `"credsStore": "desktop"` in `~/.docker/config.json`.
  With the plain engine, image pulls fail until that line is removed.
