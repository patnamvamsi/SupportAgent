# SupportAgent developer commands (10-wp WP-000).
#
# Targets named by the spec: check, test, invariants, e2e, record-sn.
#
# Suite targets are hard the moment their folder exists: while the folder
# is absent they print a notice and exit 0 (the suite lands red-first with
# its WP, 05 §2). A deleted, emptied or mis-layered suite fails instead of
# silently passing — matching the CI gate (04 §4/§6).
#
# pytest-socket disables all sockets by default (pyproject addopts); the
# DB-backed suites opt the Docker socket and localhost back in, matching CI.

UV ?= uv

.PHONY: help check test contract invariants e2e record-sn ci-local

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

check: ## Lint + format + types + architecture contracts (04 §6 hard gates)
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy --strict src tests
	$(UV) run lint-imports

test: ## Unit + property suites (no network, no Docker — 04 §1)
	$(UV) run pytest -m "unit or property"

# Each suite recipe is a single self-contained shell line: `exit 0` in one
# recipe line only ends that line's shell, so make would still run the next
# line — the guard and the run must share one line, ending in `exit $$rc`.
contract: ## Contract suites against every port implementation (04 §4)
	@rc=0; if [ ! -d tests/contract ]; then echo "tests/contract does not exist yet — it lands red-first with its WP (05 §2)"; exit 0; fi; $(UV) run pytest -m contract || rc=$$?; exit $$rc

invariants: ## INV-01..06 on SQLite AND Postgres (ARCH §19; 04 §6)
	@rc=0; if [ ! -d tests/invariants ]; then echo "tests/invariants does not exist yet — it lands red-first with its WP (05 §2)"; exit 0; fi; $(UV) run pytest -m invariant --db=sqlite --allow-unix-socket --allow-hosts=127.0.0.1,localhost || rc=$$?; exit $$rc
	@rc=0; if [ ! -d tests/invariants ]; then echo "tests/invariants does not exist yet — it lands red-first with its WP (05 §2)"; exit 0; fi; $(UV) run pytest -m invariant --db=postgres --allow-unix-socket --allow-hosts=127.0.0.1,localhost || rc=$$?; exit $$rc

e2e: ## End-to-end suites (04 §6)
	@rc=0; if [ ! -d tests/e2e ]; then echo "tests/e2e does not exist yet — it lands red-first with its WP (05 §2)"; exit 0; fi; $(UV) run pytest -m e2e --allow-unix-socket --allow-hosts=127.0.0.1,localhost || rc=$$?; exit $$rc

record-sn: ## Record ServiceNow fixtures from a developer instance (04 §3; re-record each release)
	@test -n "$(SN_HOST)" || { echo "usage: make record-sn SN_HOST=<developer-instance-host>"; exit 1; }
	@rc=0; if [ ! -d tests/integration ]; then echo "tests/integration does not exist yet — it lands red-first with its WP (05 §2)"; exit 0; fi; $(UV) run pytest -m "integration and sn" --allow-hosts="$(SN_HOST)" || rc=$$?; exit $$rc

ci-local: ## Every ci.yml job on this machine, with a pass/fail table (when Actions can't run)
	@scripts/ci-local.sh
