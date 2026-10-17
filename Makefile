# SupportAgent developer commands (10-wp WP-000).
#
# Targets named by the spec: check, test, invariants, e2e, record-sn.
# pytest exit 5 = "no tests were collected" — legal until a WP adds tests to
# that suite, so suite targets accept 0 or 5 and fail on anything else.

UV ?= uv

.PHONY: help check test contract invariants e2e record-sn

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

check: ## Lint + format + types + architecture contracts (04 §6 hard gates)
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy --strict src tests
	$(UV) run lint-imports

test: ## Unit + property suites (no network, no Docker — 04 §1)
	$(UV) run pytest -m "unit or property"

contract: ## Contract suites against every port implementation (04 §4)
	@rc=0; $(UV) run pytest -m contract || rc=$$?; if [ $$rc -eq 0 ] || [ $$rc -eq 5 ]; then exit 0; else exit $$rc; fi

invariants: ## INV-01..06 on SQLite AND Postgres (ARCH §19; 04 §6)
	@rc=0; $(UV) run pytest -m invariant --db=sqlite || rc=$$?; if [ $$rc -eq 0 ] || [ $$rc -eq 5 ]; then exit 0; else exit $$rc; fi
	@rc=0; $(UV) run pytest -m invariant --db=postgres || rc=$$?; if [ $$rc -eq 0 ] || [ $$rc -eq 5 ]; then exit 0; else exit $$rc; fi

e2e: ## End-to-end suites (04 §6)
	@rc=0; $(UV) run pytest -m e2e || rc=$$?; if [ $$rc -eq 0 ] || [ $$rc -eq 5 ]; then exit 0; else exit $$rc; fi

record-sn: ## Record ServiceNow fixtures from a developer instance (04 §3; re-record each release)
	@test -n "$(SN_HOST)" || { echo "usage: make record-sn SN_HOST=<developer-instance-host>"; exit 1; }
	@rc=0; $(UV) run pytest -m "integration and sn" --allow-hosts="$(SN_HOST)" || rc=$$?; if [ $$rc -eq 0 ] || [ $$rc -eq 5 ]; then exit 0; else exit $$rc; fi
