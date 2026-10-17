# SupportAgent Technical Specification

**Audience:** engineers and coding agents implementing SupportAgent.
**Spec version:** `tech-spec 1.0.0`. It aligns with `ARCHITECTURE.md` (frozen for v0.0) and `ROADMAP.md` (v0.0 wk 1–4 → v1.0 wk 24).
**Precedence:** [ARCHITECTURE.md](../spec/ARCHITECTURE.md) defines *what must be true*. This tech spec defines *how to build it*. If they conflict, ARCHITECTURE wins, and the conflict is a bug in this spec, fixed through an ADR ([../adr/](../adr/)).

---

## Why this exists

The design docs (`docs/spec/`) state guarantees, contracts, a data model and a roadmap. A coding agent also needs things they don't state:

| Gap in the design docs | Filled by |
|---|---|
| Exact module boundaries, dependency rules and package layout | [01-architecture-and-contracts.md](01-architecture-and-contracts.md) §1–2 |
| Interface (port) signatures, domain types with field-level validation, error taxonomy | [01-architecture-and-contracts.md](01-architecture-and-contracts.md) §3–6 |
| Exact algorithms (normalize, fingerprint, canonical hashing, idempotency keys, reconciliation markers, Wilson bound, storm grouping) | [01-architecture-and-contracts.md](01-architecture-and-contracts.md) §7 |
| `supportagent.yaml` schema, HTTP API, CLI, prompt files, ServiceNow API mapping | [02-interfaces.md](02-interfaces.md) |
| Complete DDL for every table, migration order, both SQL dialects | [03-data-and-migrations.md](03-data-and-migrations.md) |
| Test strategy, fakes, fixtures, test-ID registry, CI pipeline | [04-test-strategy.md](04-test-strategy.md) |
| Coding standards, TDD loop, design patterns, review checklist | [05-engineering-standards.md](05-engineering-standards.md) |
| Versioning of the app, schema, config, taxonomies, prompts, eval snapshots, API | [06-versioning-and-release.md](06-versioning-and-release.md) |
| Ordered, dependency-mapped work packages with acceptance tests, per release | [10-wp-v0.0.md](10-wp-v0.0.md), [11-wp-v0.1.md](11-wp-v0.1.md), [12-wp-v0.2.md](12-wp-v0.2.md), [13-wp-v0.3.md](13-wp-v0.3.md) |

**Depth by release.** v0.0 and v0.1 are specified to file and test level. v0.2 and v0.3 are specified to work-package level with acceptance tests, because the v0.0 shadow run re-sets thresholds and may reshape them (ARCH header). Each later release gets a refinement pass (`tech-spec 1.x`) before its first work package starts.

---

## How a coding agent uses this spec

1. **Pick the next work package** whose `Depends on` are all `done` (see the build order below). Never start a WP with an unmet dependency.
2. **Read its spec refs**: the WP section, the ARCH sections it cites, and the contracts in 01/02/03 it touches.
3. **Write the tests first.** Every WP lists its tests (*Tests first*). Commit them failing (`red`), then implement until green, then refactor ([05 §2](05-engineering-standards.md#2-the-tdd-loop)).
4. **Stay inside the WP's file list.** Touching a file outside it requires noting it in the PR and checking the layer rules ([01 §2](01-architecture-and-contracts.md#2-layers-and-dependency-rules)).
5. **Meet the Definition of Done** (below) and the WP's own DoD. Update the WP status table in its release file.
6. **If the spec is ambiguous or wrong, stop and write an ADR.** Don't guess. Record the question, the options and the choice in `docs/adr/`, then continue.

### Definition of Done (every WP)

- All tests the WP lists exist, have the listed IDs, and pass on **SQLite and Postgres** where they touch the database.
- `make check` is green: ruff (lint + format), mypy `--strict`, import-linter contracts, pytest with coverage thresholds ([04 §7](04-test-strategy.md#7-coverage-and-quality-thresholds)).
- No new `# type: ignore`, `noqa`, `TODO` or `xfail` without an issue link.
- Public functions and classes have docstrings stating *what* and *why*, not *how*.
- Any new event kind, action kind, config key, table, endpoint or CLI flag is added to its single source of truth (enum, schema, migration), with tests.
- Spec drift is fixed: if behaviour differs from this spec, the spec or an ADR changes in the same PR.
- The CHANGELOG is not touched by WPs. Release notes are produced at release time ([06 §6](06-versioning-and-release.md#6-release-procedure)).

---

## Build order

Work-package IDs: `WP-0xx` = v0.0, `WP-1xx` = v0.1, `WP-2xx` = v0.2, `WP-3xx` = v0.3. Lanes assume 2–3 engineers (A = platform/data, B = LLM/evals, C = integration).

### v0.0 (weeks 1–4): the trust chain

```
W1  A: WP-000 repo+CI ──► WP-001 domain kernel ──► WP-003 persistence base
    B:                    WP-002 config/catalog/taxonomy
W2  A: WP-004 trace+replay   WP-005 gate schema+triggers (INV-01)
    B: WP-007 LLMClient+redaction (INV-06)        C: WP-009 fingerprint+dedup (INV-02)
W3  A: WP-006 outbox executor+reconcile (INV-04)  C: WP-008 ServiceNow adapter+fake
    B: WP-010 classify+route+safe default         A: WP-012 identity (OIDC, users, setup)
W4  B: WP-011 Loop A pipeline   A: WP-013 approvals UI/API   C: WP-015 doctor
    B: WP-014 shadow runner+stats                 all: WP-016 smoke evals + v0.0 release
```

| WP | Title | Depends on |
|---|---|---|
| WP-000 | Repository, tooling, CI matrix | none |
| WP-001 | Domain kernel (ids, clock, hashing, kinds, tiers, errors) | 000 |
| WP-002 | Config, service catalog, taxonomies | 001 |
| WP-003 | Persistence foundation (engine, UoW, base tables) | 001 |
| WP-004 | Event store, tracing, replay (INV-03) | 003 |
| WP-005 | Gate schema and INV-01 triggers | 003, 002 |
| WP-006 | Outbox executor and reconciliation (INV-04) | 005, 004 |
| WP-007 | LLMClient, redaction, providers, fault injection (INV-06) | 001, 004 |
| WP-008 | ServiceNow adapter, poll sync, fake ServiceNow | 002, 003 |
| WP-009 | Normalization, fingerprint, dedup (INV-02) | 003, 001 |
| WP-010 | Pre-classifier, classify slot, calibration, routing, safe default | 007, 002 |
| WP-011 | Loop A pipeline and propose | 009, 010, 005, 008 |
| WP-012 | Identity: OIDC, users, sessions, first-run setup | 005 |
| WP-013 | Approvals API and admin UI | 012, 006 |
| WP-014 | Shadow runner, counterfactual metrics, `stats` | 011 |
| WP-015 | `doctor` (checks, setup, automation discovery) | 008, 012 |
| WP-016 | Smoke eval harness, invariant gate, v0.0 release | 011, 013, 014, 015 |

### v0.1 (weeks 5–12)

| WP | Title | Depends on | Week |
|---|---|---|---|
| WP-101 | Case lifecycle, takeover, hand-back | 008, 011 | 5–6 |
| WP-102 | Comms plans, renderer, cadence scheduler | 006, 013 | 5–7 |
| WP-103 | RBAC roles | 012 | 6 |
| WP-104 | Quality eval harness (N samples, Wilson, cost matrix, snapshot, cache) | 016 | 6–8 |
| WP-105 | Calibration table generation | 104 | 8 |
| WP-106 | Resilience: retries, breakers, degraded mode, fault suite, model deprecation | 007, 008, 006 | 7–9 |
| WP-107 | First-party read tools + one change source | 002 | 8–9 |
| WP-108 | Case memory and retrieval | 009, 004 | 8–9 |
| WP-109 | Feedback capture | 101 | 9 |
| WP-110 | RCA-lite (runbooks, CEL, probes, reduce, entailment) | 107, 007 | 8–10 |
| WP-111 | Storm grouping and spend breaker | 009, 011 | 9–10 |
| WP-112 | `enrich_page` and `page_recommended` | 107, 011 | 10 |
| WP-113 | Generic webhook adapter (HMAC) | 009 | 10 |
| WP-114 | Storm soak gate | 111, 006, 102 | 10–11 |
| WP-115 | Shadow promotion gate, stats, quickstart, Litestream ops | 104, 014 | 11–12 |
| WP-116 | v0.1 release | all of the above | 12 |

v0.2 (weeks 13–18) and v0.3 (weeks 19–24): see [12-wp-v0.2.md](12-wp-v0.2.md) and [13-wp-v0.3.md](13-wp-v0.3.md).

---

## Files in this spec

| File | Contents |
|---|---|
| [01-architecture-and-contracts.md](01-architecture-and-contracts.md) | Package layout, layers, ports, domain types, errors, algorithms |
| [02-interfaces.md](02-interfaces.md) | Config schema, HTTP API, admin UI, CLI, prompts, ServiceNow mapping |
| [03-data-and-migrations.md](03-data-and-migrations.md) | Full DDL, triggers in both dialects, migration sequence |
| [04-test-strategy.md](04-test-strategy.md) | Test layers, fakes, fixtures, test-ID registry, CI pipeline |
| [05-engineering-standards.md](05-engineering-standards.md) | TDD, clean code, patterns, error handling, review checklist |
| [06-versioning-and-release.md](06-versioning-and-release.md) | Every versioned artifact and the release procedure |
| [10-wp-v0.0.md](10-wp-v0.0.md) | v0.0 work packages |
| [11-wp-v0.1.md](11-wp-v0.1.md) | v0.1 work packages |
| [12-wp-v0.2.md](12-wp-v0.2.md) | v0.2 work packages |
| [13-wp-v0.3.md](13-wp-v0.3.md) | v0.3 / v1.0 work packages |
