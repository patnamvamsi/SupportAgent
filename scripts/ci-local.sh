#!/usr/bin/env bash
# The job functions run through `job`, which shellcheck can't follow.
# shellcheck disable=SC2317
# Run every job of .github/workflows/ci.yml on this machine, in the same order
# and with the same commands, and print a pass/fail table.
#
# Use it when GitHub Actions can't run (minutes used up, billing): post the
# table on the PR before merging. Needs uv, Docker (Postgres testcontainers
# and the image job) and gitleaks on PATH.
#
#   scripts/ci-local.sh            # all jobs, compared against origin/main
#   BASE=origin/some-branch scripts/ci-local.sh
#
# Keep this list in step with ci.yml: a job added there and not here is a
# gate a local run silently skips.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
BASE=${BASE:-origin/main}
# shellcheck disable=SC2054 # the comma is inside one flag's value
NET=(--allow-unix-socket --allow-hosts=127.0.0.1,localhost)
LOG_DIR=${LOG_DIR:-$(mktemp -d)}
mkdir -p "$LOG_DIR"
results=()
failed=0

job() {
  local name=$1
  shift
  local log="$LOG_DIR/$name.log"
  local start=$SECONDS
  if "$@" >"$log" 2>&1; then
    results+=("ok    $name ($((SECONDS - start))s)")
  else
    results+=("FAIL  $name ($((SECONDS - start))s) — $log")
    failed=1
  fi
}

lint() { uv run ruff check . && uv run ruff format --check .; }
typecheck() { uv run mypy --strict src tests; }
architecture() { uv run lint-imports; }
unit() { uv run pytest -m "unit or property" --cov --cov-report=term-missing; }
contract() { uv run pytest -m contract; }
invariants() { uv run pytest -m invariant --db="$1" "${NET[@]}"; }
integration() {
  uv run pytest -m integration --db="$1" "${NET[@]}" &&
    uv run pytest -m soak --db="$1" "${NET[@]}"
}
e2e() { uv run pytest -m e2e "${NET[@]}"; }
evals() {
  uv run supportagent eval run --suite smoke &&
    uv run supportagent eval lab --pack common --config evals/lab/lab.yaml --split all &&
    uv run supportagent eval lab --pack python-postgres --config evals/lab/lab.yaml --split all &&
    if [ -d evals/entail ]; then uv run supportagent eval entail --config evals/lab/lab.yaml; fi &&
    if [ -d evals/onboarding ]; then uv run supportagent eval onboarding; fi
}
security() {
  local reqs
  reqs=$(mktemp)
  uv export --frozen --no-dev --format requirements-txt --no-hashes \
    --no-emit-project -o "$reqs" &&
    uv run pip-audit -r "$reqs" --no-deps --disable-pip &&
    uv run bandit -q -r src &&
    gitleaks detect --redact --log-opts="$BASE..HEAD" &&
    docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:v0.10.0 install.sh scripts/ci-local.sh
}
image() {
  docker build -f docker/Dockerfile -t supportagent:ci . &&
    [ "$(docker run --rm supportagent:ci --version)" = "0.0.0" ]
}

uv sync --frozen >/dev/null || exit 1
echo "commit $(git rev-parse --short HEAD) vs $BASE; logs in $LOG_DIR"
job lint lint
job typecheck typecheck
job architecture architecture
job unit unit
job contract contract
job invariants-sqlite invariants sqlite
job invariants-postgres invariants postgres
job integration-sqlite integration sqlite
job integration-postgres integration postgres
job e2e e2e
job eval evals
job security security
job image image

printf '%s\n' "${results[@]}"
exit $failed
