# 04. Test Strategy

Implements: ARCH §2, §11, §19. Every invariant in ARCH §2 is a test before it is code.

---

## 1. Test layers

| Layer | Marker | Scope | Speed budget | Runs |
|---|---|---|---|---|
| Unit | `unit` | `domain/` pure functions, `app/` services with fakes | < 5 ms each; suite < 30 s | every commit |
| Property | `property` | Hypothesis tests for `normalize`, `fingerprint`, `canonical_json`, state machines, `wilson_lower_bound`, `reduce`, grouping | suite < 60 s | every commit |
| Contract | `contract` | One suite per port, run against the fake **and** the real adapter (§4) | < 2 min | every PR |
| DB / invariant | `invariant` | INV-01..06 tests against a real DB, parametrized over `sqlite` and `postgres` | < 3 min | every PR (hard gate) |
| Integration | `integration` | Use case + real DB + fake external systems, through `bootstrap` | < 5 min | every PR |
| End-to-end | `e2e` | `supportagent run` in Docker + FakeServiceNow HTTP server + FakeOIDC; drives the UI via HTTP | < 10 min | every PR to `main` |
| Fault injection | `faults` | `SUPPORTAGENT_FAULT_INJECT=...` scenarios (INV-05) | < 5 min | every PR from v0.1 |
| Soak | `soak` | 1,000-alert storm (ARCH §19) | < 15 min | nightly + release |
| Eval smoke | `eval_smoke` | 25–50 cases, fixture LLM for wiring + real LLM for calibration | < 5 min | every PR touching `prompts/`, `taxonomy/`, `routing` |
| Eval quality | `eval_quality` | 150–250-case holdout, real LLM, N samples (v0.1) | < 45 min | PRs touching prompts or models; nightly |

`pytest -m "unit or property"` must pass with **no network and no Docker**. A unit test that reaches the network fails, because `pytest-socket` blocks it.

---

## 2. Test layout

```
tests/
├── conftest.py                 global fixtures (§3)
├── unit/<mirror of src/supportagent>/test_*.py
├── property/test_*.py
├── contract/
│   ├── ticket_source_contract.py   abstract suite (a class with test_* methods)
│   ├── test_servicenow_fake.py     subclasses the suite with FakeServiceNow
│   └── test_servicenow_http.py     subclasses it with ServiceNowSource + respx-recorded fixtures
├── invariants/
│   ├── test_inv01_gate.py  test_inv02_dedup.py  test_inv03_trace.py
│   ├── test_inv04_idempotency.py  test_inv05_safe_default.py  test_inv06_redaction.py
├── integration/  e2e/  faults/  soak/
└── data/                       golden files, recorded HTTP fixtures, canary corpus
```

Naming: test functions are `test_<behaviour>_<condition>`, for example `test_gate_rejected_approval_blocked`. The **test IDs in ARCH §19 and in the WP files are the exact function names**. `test_spec_test_ids_exist` greps the spec for `test_[a-z0-9_]+` IDs and fails if any is missing from the suite (from WP-016 on).

---

## 3. Fixtures and fakes

| Fixture | Provides | Notes |
|---|---|---|
| `clock` | `FrozenClock(start=2026-10-20T09:00:00Z)` with `.advance(seconds)` | Code never calls `datetime.now()` directly; lint rule `DTZ` plus an import ban |
| `ids` | `SequentialIdGenerator` → `case_0001`, ... | Deterministic snapshots |
| `db` | Parametrized `sqlite` (tmp file, WAL) and `postgres` (testcontainers `postgres:16`, one per session, schema reset per test via `TRUNCATE`) | Migrations applied once per session at head |
| `uow` | `SqlUnitOfWork(db)` | |
| `events` | Captures `EventSink.append` into a list; `.kinds()` helper | Unit level |
| `fake_sn` | `FakeServiceNow`: in-memory tables, `sys_audit` / `sys_journal_field` simulation, `sys_mod_count`, configurable 429/5xx injection, same-second timestamp mode | Also runs as an HTTP server (`fake_sn_http`) for e2e |
| `fake_llm` | `FakeLLMProvider(script={slot: [responses...]})`; records every `ProviderRequest` in `.captured` | INV-06 asserts over `.captured` |
| `fake_oidc` | Local issuer with signed id tokens for named test users (`alice`, `bob`, `carol` = admin) | |
| `catalog` / `taxonomy` / `config` | Loaded from `tests/data/fixtures/*.yaml` | |
| `container` | `bootstrap.build_container(config, overrides={...})` with fakes injected | Integration tests use the real composition root |

**Builders** (test data): `an_alert(**overrides)`, `a_ticket(**overrides)`, `a_proposal(...)`, `an_approval(...)`, `a_grant(...)`, all in `tests/builders.py`. Tests state only the fields that matter to the behaviour under test. Hard-coded literals elsewhere are discouraged.

**Never mock what you own.** Use the fakes for ports. `unittest.mock` is allowed only for third-party SDK edges inside adapter unit tests.

---

## 4. Contract tests

Each port has one abstract contract suite. Every implementation, fake and real, must pass the same suite, which keeps the fakes honest.

| Port | Implementations under contract | Real-side source |
|---|---|---|
| `TicketSource` / `TicketSink` | `FakeServiceNow`, `ServiceNowSource/Sink` | `respx` fixtures recorded once from a ServiceNow developer instance (`make record-sn`), re-recorded each release |
| `LLMProvider` | `FakeLLMProvider`, `AnthropicProvider`, `OpenAIProvider`, `OllamaProvider` | Recorded fixtures; the live call is gated behind `--live-llm` |
| `UnitOfWork` + repos | SQLite, Postgres | Real engines |
| `IdentityProvider` | `FakeOIDC`, `OIDCProvider` | `FakeOIDC` issuer over HTTP |
| `Tool` (v0.1) | `FakeTool`, each first-party tool | Recorded fixtures |

Example contract case: `test_poll_returns_records_updated_in_same_second_as_cursor` (02 §6 overlap rule).

---

## 5. Test-ID registry (invariants and cross-cutting)

| ID | Layer | Given / When / Then | WP |
|---|---|---|---|
| `test_fk_pragma_on_every_connection` | invariant | New SQLite connection → `PRAGMA foreign_keys` = 1 | 003 |
| `test_dedup_single_case` | invariant | 5 identical alerts → 1 case, `duplicate_count` = 4 | 009 |
| `test_dedup_concurrent_no_second_case` | invariant | 20 threads (SQLite) / 20 connections (PG) upsert the same fingerprint → 1 case | 009 |
| `test_dedup_human_owned_no_second_case` | invariant | Case `human_owned`, duplicate arrives → same case | 009 |
| `test_human_tickets_never_collapse` | invariant | Two tickets, same service/category, different numbers → 2 cases | 009 |
| `test_every_action_traced` | invariant | Run a triage end-to-end → events exist for ingest, dedup, preclass, classify, decision, gate, send with one trace id | 004/011 |
| `test_gate_rejected_approval_blocked` | invariant | Approval `rejected` → outbox insert raises `AuthorizationDenied` | 005 |
| `test_gate_expired_approval_blocked` | invariant | Proposal `expired` after approval → ABORT | 005 |
| `test_gate_payload_hash_mismatch_blocked` | invariant | Outbox hash ≠ approval hash → ABORT | 005 |
| `test_gate_grant_expired_or_out_of_scope_blocked` | invariant | Grant past `valid_to_ts`, or scope differs → ABORT | 005 |
| `test_gate_grant_pending_not_enforceable` | invariant | Grant `requested` → ABORT | 005 |
| `test_gate_approval_single_use` | invariant | Second outbox row with the same approval → ABORT | 005 |
| `test_outbox_immutable_after_insert` | invariant | UPDATE of `payload_json` → ABORT; UPDATE of `status` → ok | 005 |
| `test_customer_comm_never_by_policy_grant` | invariant | Approved grant for `customer_comment` → ABORT | 005 |
| `test_comms_plan_revoked_blocked` | invariant | Plan `revoked` → ABORT | 005 |
| `test_create_page_never_executable` | invariant | Any path with `create_page` → ABORT | 005 |
| `test_self_approval_blocked` | invariant | Approver = proposal creator → ABORT; grant approver = requester → CHECK fails | 005 |
| `test_trigger_kind_lists_match_code` | invariant | Trigger SQL literals == `domain.kinds` sets | 005 |
| `test_executor_rehash_mismatch_aborts` | invariant | Corrupt `payload_json` bypassing triggers (raw SQL with triggers disabled in the test) → executor marks failed, emits `executor_hash_mismatch` | 006 |
| `test_idempotent_replay` | invariant | Crash after the external send, before `mark_done` → retry finds the marker and sends nothing | 006 |
| `test_fault_injection_suite` | faults | Each fault in `SUPPORTAGENT_FAULT_INJECT` → case ends safe-default, no unapproved side effect, no crash | 007/106 |
| `test_llm_bound_canaries_absent` | invariant | One canary per detector class in ticket body and tool output → none in `fake_llm.captured` | 007 |
| `test_plan_autosend_fields_from_record_state` | invariant | Plan field declared `source: agent` → config/plan validation fails; agent-inferred `resolved` never renders | 102 |
| `test_sync_same_second_not_missed` | contract | 3 updates sharing the cursor second → all processed once | 008 |
| `test_sync_human_change_behind_echo_detected` | contract | Human state change then integration work note in one poll → takeover detected | 008/101 |
| `test_sync_automation_assign_not_takeover` | contract | Assignment by an automation actor → no takeover | 101 |
| `test_docs_action_kinds_canonical` | unit | Every backticked snake_case kind in `docs/` is in `ActionKind` | 001 |
| `test_changelog_anchors_resolve` | unit | Every "§x.y" cited in `docs/CHANGELOG.md` exists as a heading | 000 |
| `test_spec_test_ids_exist` | unit | Every `test_*` ID in `docs/spec` and `docs/tech-spec` exists | 016 |
| `test_schema_enums_match_domain` | invariant | DB CHECK lists == domain StrEnums | 003 |
| `test_migrations_roundtrip` | invariant | upgrade → downgrade → upgrade on both engines | 003 |

WP files list their remaining behaviour tests.

---

## 6. CI pipeline (GitHub Actions; jobs and gates)

```
lint ─► typecheck ─► unit+property ─► contract ─┬─► invariants[sqlite] ─┬─► integration ─► e2e ─► (main) image build
                                               └─► invariants[postgres]┘
paths(prompts/**, taxonomy/**, routing, llm config) ─► eval_smoke (v0.0) / eval_quality (v0.1+)
nightly ─► soak, eval_quality full, mutation testing (domain/), re-record check
```

| Job | Command | Gate |
|---|---|---|
| lint | `ruff check . && ruff format --check .` | hard |
| typecheck | `mypy --strict src tests` | hard |
| architecture | `lint-imports` | hard |
| unit+property | `pytest -m "unit or property" --cov --cov-fail-under=…` | hard |
| contract | `pytest -m contract` | hard |
| invariants | `pytest -m invariant --db=sqlite` and `--db=postgres` | hard (merge blocked) |
| integration / e2e | `pytest -m integration` / `pytest -m e2e` | hard |
| eval | `supportagent eval run --suite smoke` (v0.0) / `--suite quality` (v0.1) | hard when triggered by path |
| security | `pip-audit`, `bandit -r src`, `gitleaks` | hard |
| soak (nightly) | `pytest -m soak` | blocks release |

---

## 7. Coverage and quality thresholds

| Package | Branch coverage | Mutation score (nightly, `mutmut`) |
|---|---|---|
| `domain/` | ≥ 95% | ≥ 80% |
| `app/` | ≥ 90% | reported |
| `adapters/` | ≥ 80% | — |
| overall | ≥ 85% | — |

Coverage is a floor, not a target. A test that only executes lines without asserting behaviour is a review failure.

---

## 8. Eval suites (summary; detail in WP-016 and WP-104)

- **Case format:** `evals/cases/<id>.yaml` with `source`, `input` (an `AlertEvent` as YAML), `expect` (`classification`, `severity`, `actions`, `comms_contains`, `outcome`), and `meta.why` when edited.
- **Smoke (v0.0):** 25–50 cases, including **≥ 12 per v0.0 routing label**. It runs the full pipeline with the fixture LLM (wiring) and, separately, the real LLM (`--write-calibration` produces the `grade: smoke` table).
- **Quality (v0.1):** a 150–250-case holdout, N = 3 samples, per-case majority, Wilson LB over cases per severity band, cost matrix, frozen snapshot, cache key `rendered_prompt_hash + model_id + resolved_version + snapshot_id`.
