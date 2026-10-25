#!/usr/bin/env bash
# Keycloak for staging (runbook step 3, BL-023): realm `supportagent`, a
# confidential client for SupportAgent, and two users: ada (first login, so
# she becomes the admin) and bob (made an approver by ada in the UI).
#
#   cd docker/staging && ./keycloak-setup.sh
#
# Reads KEYCLOAK_ADMIN_* and STAGING_USER_PASSWORD from .env, and writes the
# client secret back to .env as OIDC_CLIENT_SECRET. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
# One key from .env, without shell-parsing the file (values may hold shell
# metacharacters; compose reads them single-quoted, i.e. literally).
envval() { sed -n "s/^$1=//p" .env | tail -1 | sed "s/^'\(.*\)'$/\1/"; }
KEYCLOAK_ADMIN_USER=$(envval KEYCLOAK_ADMIN_USER)
KEYCLOAK_ADMIN_PASSWORD=$(envval KEYCLOAK_ADMIN_PASSWORD)
STAGING_USER_PASSWORD=$(envval STAGING_USER_PASSWORD)
: "${KEYCLOAK_ADMIN_USER:?}" "${KEYCLOAK_ADMIN_PASSWORD:?}" "${STAGING_USER_PASSWORD:?}"

kc() { docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh "$@"; }

until curl -fsS http://localhost:8880/realms/master >/dev/null; do sleep 2; done
kc config credentials --server http://localhost:8080 --realm master \
  --user "$KEYCLOAK_ADMIN_USER" --password "$KEYCLOAK_ADMIN_PASSWORD"

kc get realms/supportagent >/dev/null 2>&1 || kc create realms -s realm=supportagent -s enabled=true

if [ -z "$(kc get clients -r supportagent -q clientId=supportagent --fields id --format csv --noquotes)" ]; then
  kc create clients -r supportagent \
    -s clientId=supportagent -s enabled=true -s publicClient=false \
    -s standardFlowEnabled=true -s directAccessGrantsEnabled=false \
    -s 'redirectUris=["http://192.168.0.201:8800/auth/callback"]' \
    -s 'attributes={"pkce.code.challenge.method":"S256"}'
fi
cid=$(kc get clients -r supportagent -q clientId=supportagent --fields id --format csv --noquotes)
secret=$(kc get "clients/$cid/client-secret" -r supportagent --fields value --format csv --noquotes)

for who in ada bob; do
  name=$([ "$who" = ada ] && echo "Ada Admin" || echo "Bob Approver")
  if [ -z "$(kc get users -r supportagent -q username=$who --fields id --format csv --noquotes)" ]; then
    kc create users -r supportagent -s username=$who -s enabled=true \
      -s email=$who@staging.test -s emailVerified=true \
      -s firstName="${name%% *}" -s lastName="${name#* }"
  fi
  kc set-password -r supportagent --username $who --new-password "$STAGING_USER_PASSWORD"
done

if grep -q '^OIDC_CLIENT_SECRET=' .env; then
  sed -i "s|^OIDC_CLIENT_SECRET=.*|OIDC_CLIENT_SECRET='$secret'|" .env
else
  echo "OIDC_CLIENT_SECRET='$secret'" >> .env
fi
echo "keycloak ready: realm supportagent, client supportagent, users ada and bob"
