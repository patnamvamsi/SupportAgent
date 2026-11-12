#!/usr/bin/env bash
# Smoke-test a built SupportAgent image on its own, with no checkout mounted, the
# way a cloud container service runs it:
#
#   1. `--version` prints the version in pyproject.toml (or $2);
#   2. the offline smoke eval passes from the content the image carries;
#   3. `migrate` and `serve` start on SQLite with the example config and catalog,
#      and /healthz and /readyz answer 200.
#
#   scripts/image-smoke.sh supportagent:ci
#   scripts/image-smoke.sh ghcr.io/owner/supportagent@sha256:... 0.1.0
#
# The credentials are dummies: nothing here reaches ServiceNow, an IdP or a model.
set -euo pipefail

image=${1:?usage: image-smoke.sh IMAGE [VERSION]}
cd "$(dirname "$0")/.."
expected=${2:-$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)}

got=$(docker run --rm "$image" --version)
if [ "$got" != "$expected" ]; then
  echo "::error::$image --version printed '$got', expected '$expected'"
  exit 1
fi
echo "ok    version $got"

docker run --rm "$image" eval run --suite smoke --artifacts /tmp/reports | tail -1
echo "ok    offline smoke eval from the image's own content"

name="sa-smoke-$$"
port=${SMOKE_PORT:-18800}
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$name" -p "127.0.0.1:$port:8000" \
  -e SUPPORTAGENT_DB_URL=sqlite:////tmp/sa.db \
  -e SA_MASTER_KEY_ENV="$(printf '0%.0s' $(seq 64))" \
  -e SA_SESSION_SECRET="$(printf 's%.0s' $(seq 40))" \
  -e SN_CLIENT_ID=x -e SN_CLIENT_SECRET=x -e OIDC_CLIENT_ID=x -e OIDC_CLIENT_SECRET=x \
  -e ANTHROPIC_API_KEY=x -e PAGERDUTY_TOKEN=x \
  --entrypoint sh "$image" -c '
    sed "s#catalog: catalog/services.yaml#catalog: catalog/services.example.yaml#" \
      supportagent.example.yaml > /tmp/smoke.yaml &&
    supportagent migrate --config /tmp/smoke.yaml &&
    exec supportagent serve --config /tmp/smoke.yaml --host 0.0.0.0 --port 8000' >/dev/null

for path in healthz readyz; do
  code=000
  for _ in $(seq 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/$path" || true)
    [ "$code" = 200 ] && break
    sleep 1
  done
  if [ "$code" != 200 ]; then
    echo "::error::/$path answered $code"
    docker logs "$name" 2>&1 | tail -30
    exit 1
  fi
  echo "ok    /$path 200"
done
