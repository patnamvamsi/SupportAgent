#!/usr/bin/env bash
# Upgrade test (06 §7: data written by a release stays readable after upgrading).
#
# Checks out the previous release, lets *its* code create a database and fill
# it (migrate, then `shadow import` of 50 incidents: cases, events, shadow
# records), then upgrades that database with this tree's code and checks that:
#   - `migrate` reaches this build's head;
#   - no case, event or shadow record was lost;
#   - this build reads it back: `stats` over the imported shadow records.
#
#   scripts/upgrade-test.sh                    # from the newest earlier v* tag, SQLite
#   scripts/upgrade-test.sh --db postgres      # the same on Postgres 16 (Docker)
#   scripts/upgrade-test.sh --from <ref>       # from any commit, e.g. before a migration
#
# With no earlier release and no --from, it says so and passes. The model is
# unreachable on purpose: classification fails closed, which still writes
# every row, and nothing leaves the machine.
set -euo pipefail

cd "$(dirname "$0")/.."
here=$PWD
from=""
db=sqlite
while [ $# -gt 0 ]; do
  case $1 in
    --from) from=$2; shift 2 ;;
    --db) db=$2; shift 2 ;;
    *) echo "usage: upgrade-test.sh [--from REF] [--db sqlite|postgres]" >&2; exit 2 ;;
  esac
done

if [ -z "$from" ]; then
  current=$(git describe --tags --exact-match --match 'v*' HEAD 2>/dev/null || true)
  from=$(git tag --list 'v*' --merged HEAD --sort=-v:refname | grep -vx "${current:-none}" | head -1 || true)
  if [ -z "$from" ]; then
    echo "ok    no earlier release to upgrade from (skipped)"
    exit 0
  fi
fi
echo "upgrade from $from ($(git rev-parse --short "$from^{commit}")) to $(git rev-parse --short HEAD), $db"

work=$(mktemp -d)
pg=""
cleanup() {
  git -C "$here" worktree remove --force "$work/from" >/dev/null 2>&1 || true
  [ -n "$pg" ] && docker rm -f "$pg" >/dev/null 2>&1
  rm -rf "$work"
}
trap cleanup EXIT

if [ "$db" = postgres ]; then
  pg="sa-upgrade-$$"
  docker run -d --name "$pg" -e POSTGRES_PASSWORD=upgrade -e POSTGRES_DB=sa \
    -p 127.0.0.1::5432 postgres:16 >/dev/null
  port=$(docker port "$pg" 5432/tcp | head -1 | sed 's/.*://')
  for _ in $(seq 60); do
    docker exec "$pg" pg_isready -U postgres -d sa >/dev/null 2>&1 && break
    sleep 1
  done
  sleep 2
  url="postgresql+psycopg://postgres:upgrade@127.0.0.1:$port/sa"
else
  url="sqlite:///$work/sa.db"
fi

export SUPPORTAGENT_DB_URL=$url SN_CLIENT_ID=x SN_CLIENT_SECRET=x OIDC_CLIENT_ID=x \
  OIDC_CLIENT_SECRET=x PAGERDUTY_TOKEN=x
SA_SESSION_SECRET=$(printf 's%.0s' $(seq 40))
SA_MASTER_KEY_ENV=$(printf '0%.0s' $(seq 64))
export SA_SESSION_SECRET SA_MASTER_KEY_ENV

# A tree's example config, runnable here: the example catalog, and a model
# nobody listens on.
config() {
  sed -e 's#catalog: catalog/services.yaml#catalog: catalog/services.example.yaml#' \
    -e 's#^  provider: anthropic.*#  provider: ollama#' \
    -e 's#^  base_url: null.*#  base_url: http://127.0.0.1:9#' \
    -e 's#^  api_key_env: ANTHROPIC_API_KEY.*#  api_key_env: null#' \
    "$1/supportagent.example.yaml" >"$2"
}

count() {
  uv run --project "$here" --frozen python - "$url" <<'PY'
import sys
import sqlalchemy as sa

engine = sa.create_engine(sys.argv[1])
with engine.connect() as conn:
    print(" ".join(
        str(conn.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar())
        for t in ("cases", "events", "shadow_records")
    ))
PY
}

git worktree add --quiet --detach "$work/from" "$from"
config "$work/from" "$work/from.yaml"
(
  cd "$work/from"
  uv sync --frozen --quiet
  uv run --frozen supportagent migrate --config "$work/from.yaml" >/dev/null 2>&1
  uv run --frozen supportagent shadow import tests/data/shadow/incidents.jsonl \
    --config "$work/from.yaml" | tail -1
)
read -r cases events shadow <<<"$(count)"
echo "ok    $from wrote $cases cases, $events events, $shadow shadow records"
if [ "$cases" -eq 0 ] || [ "$shadow" -eq 0 ]; then
  echo "::error::the earlier release wrote nothing to upgrade"
  exit 1
fi

config "$here" "$work/head.yaml"
uv run --project "$here" --frozen supportagent migrate --config "$work/head.yaml" >/dev/null
revision=$(uv run --project "$here" --frozen python - "$url" <<'PY'
import sys
import sqlalchemy as sa

with sa.create_engine(sys.argv[1]).connect() as conn:
    print(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar())
PY
)
head=$(python3 scripts/release_check.py --head)
if [ "$revision" != "$head" ]; then
  echo "::error::after migrate the database is at $revision, this build's head is $head"
  exit 1
fi
echo "ok    migrated to $head"

read -r cases2 events2 shadow2 <<<"$(count)"
if [ "$cases2" -lt "$cases" ] || [ "$events2" -lt "$events" ] || [ "$shadow2" -lt "$shadow" ]; then
  echo "::error::rows lost in the upgrade: cases $cases→$cases2, events $events→$events2, shadow $shadow→$shadow2"
  exit 1
fi
echo "ok    nothing lost: $cases2 cases, $events2 events, $shadow2 shadow records"

uv run --project "$here" --frozen supportagent stats --mode shadow --window all \
  --config "$work/head.yaml" >"$work/stats.txt"
if ! grep -q "$shadow" "$work/stats.txt"; then
  echo "::error::stats after the upgrade doesn't show the $shadow imported records"
  cat "$work/stats.txt"
  exit 1
fi
echo "ok    this build reads the upgraded data (stats over $shadow shadow records)"
