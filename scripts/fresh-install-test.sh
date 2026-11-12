#!/usr/bin/env bash
# Fresh-install test: the README's quickstart, unattended, on a clean checkout.
#
# Runs ./install.sh with an answers file (the demo ServiceNow, a local model
# nobody listens on, calibration skipped), then checks what a new user sees:
#   - SupportAgent answers /healthz and /readyz;
#   - /ui sends a signed-out user to sign in;
#   - Keycloak serves the supportagent realm's OIDC discovery;
#   - ./install.sh status and doctor run.
# Then it removes everything it created, local/ included.
#
#   scripts/fresh-install-test.sh          # refuses if local/ exists (a real install)
#
# Its own compose project, image and ports (18810 app, 18890 Keycloak): an
# install already on this machine is never touched.
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -e local/supportagent.yaml ]; then
  echo "::error::local/ already holds an install; run this on a clean checkout"
  exit 2
fi
app_port=18810
kc_port=18890
export COMPOSE_PROJECT_NAME="sa-fresh-$$" SA_IMAGE="supportagent:fresh-$$"
answers=$(mktemp -d)/answers.json
cat >"$answers" <<EOF
{
  "team_name": "Fresh Install",
  "llm": {"kind": "ollama", "model_id": "qwen/qwen3.8-27b", "base_url": "http://127.0.0.1:9"},
  "servicenow": {"kind": "demo"},
  "admin_user": "ada",
  "admin_password": "fresh-install-test",
  "app_port": $app_port,
  "keycloak_port": $kc_port
}
EOF

cleanup() {
  if [ -f local/.env ]; then
    docker compose -f docker/local/compose.yaml --env-file local/.env \
      --profile demo down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  docker image rm "$SA_IMAGE" >/dev/null 2>&1 || true
  rm -rf local "$answers"
}
trap cleanup EXIT

./install.sh --answers "$answers" --skip-calibration

expect() { # $1 what, $2 url, $3 the accepted HTTP codes (a regex)
  local code=000
  for _ in $(seq 90); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$2" || true)
    [[ $code =~ ^($3)$ ]] && { echo "ok    $1 ($code)"; return 0; }
    sleep 2
  done
  echo "::error::$1: $2 answered $code"
  ./install.sh logs app 2>&1 | tail -40 || true
  return 1
}
expect "SupportAgent /healthz" "http://127.0.0.1:$app_port/healthz" 200
expect "SupportAgent /readyz" "http://127.0.0.1:$app_port/readyz" 200
expect "the UI sends a signed-out user to sign in" "http://127.0.0.1:$app_port/ui" "302|303|307"
expect "Keycloak's supportagent realm" \
  "http://127.0.0.1:$kc_port/realms/supportagent/.well-known/openid-configuration" 200
./install.sh status
./install.sh doctor || true # the model is unreachable on purpose: doctor reports it
echo "ok    fresh install"
