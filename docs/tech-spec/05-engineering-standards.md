# 05. Engineering Standards

These rules are binding for every PR, human or agent. CI enforces most of them; the review checklist (§8) covers the rest.

---

## 1. Toolchain

| Concern | Tool | Config |
|---|---|---|
| Env and deps | `uv` (lockfile committed) | `pyproject.toml`, `uv.lock` |
| Lint + format | `ruff` | rule sets: `E,F,W,I,N,UP,B,C4,SIM,RET,PTH,DTZ,S,BLE,TRY,PL,RUF,ANN`; line length 100 |
| Types | `mypy --strict` | `disallow_any_explicit` in `domain/` and `ports/` |
| Architecture | `import-linter` | contracts in 01 §2 |
| Tests | `pytest`, `pytest-cov`, `hypothesis`, `respx`, `pytest-socket`, `testcontainers`, `mutmut` | 04 |
| Security | `bandit`, `pip-audit`, `gitleaks` | CI job `security` |
| Pre-commit | ruff, mypy (changed files), gitleaks, `lint-imports` | `.pre-commit-config.yaml` |
| Task runner | `make check` = lint + typecheck + architecture + unit/property + contract + invariants[sqlite] | |

Runtime dependencies are limited to: `fastapi`, `uvicorn`, `jinja2`, `pydantic`, `pydantic-settings`, `sqlalchemy`, `alembic`, `psycopg[binary]`, `httpx`, `typer`, `structlog`, `cryptography`, `python-ulid`, `authlib` (OIDC), `cel-python` (v0.1), and the provider SDKs `anthropic` / `openai` (behind adapters). Adding any other runtime dependency needs an ADR.

---

## 2. The TDD loop

Every behaviour change follows **red → green → refactor**, in separate commits where practical:

1. **Red.** Write the WP's listed test (exact ID) and any unit tests for the smallest next behaviour. Run it and see it fail *for the right reason* (an assertion, not an import error). Commit `test(<area>): <behaviour> [red]`.
2. **Green.** Write the minimum production code that passes. No speculative parameters, no unused branches. Commit `feat(<area>): <behaviour>`.
3. **Refactor.** With tests green, remove duplication, improve names and extract functions. No behaviour change. Commit `refactor(<area>): ...`.
4. Repeat until the WP's test list is green, then run `make check`.

Rules:
- **Outside-in for use cases:** start from the WP's acceptance or invariant test (integration level), then drop to unit tests for each domain rule it needs.
- **Inside-out for algorithms:** `normalize`, `fingerprint`, `wilson_lower_bound` and the state machines start with golden and property tests.
- **A bug fix starts with a failing test that reproduces it.** The test name describes the bug's condition.
- **Never weaken a test to make it pass.** Changing an assertion needs the reason in the commit message. For eval cases, it needs a `meta.why` note (ROADMAP §2.1).
- **Tests are deterministic.** Inject the clock, the id generator and randomness. No `sleep`; advance `FrozenClock` instead. Async waits use event-driven polling with a timeout, no fixed sleeps.

---

## 3. Clean code rules

**Naming**
- Names say what something *is* or *does* in domain language from the spec: `upsert_by_fingerprint`, `calibrated_score`, `human_owned`. Don't use `data`, `info`, `helper`, `manager`, `util` or `process`.
- Booleans read as predicates (`is_customer_facing`, `has_agreement`). Functions that return `None` on failure end with `_or_none`, unless the port contract already says so (`structured()`).

**Functions and classes**
- A function does one thing at one level of abstraction. Soft limits: 30 lines per function, 4 parameters (use a frozen dataclass beyond that), cyclomatic complexity ≤ 8 (ruff `C901`, `PLR0912`).
- Classes have one reason to change. Services are verbs (`TriageService`), domain models are nouns, ports are capabilities (`TicketSource`).
- No flag arguments that switch behaviour (`send(dry_run=True)`). Use two functions, or a Strategy.
- Prefer pure functions in `domain/`; keep I/O at the edges (functional core, imperative shell).

**Data**
- Domain models are immutable (`frozen=True`). Changes produce new instances (`model_copy(update=...)`).
- No `dict[str, Any]` across a module boundary except `JsonValue` payloads, which have a schema.
- No primitive obsession for identifiers: `CaseId = NewType("CaseId", str)` etc. in `domain/ids.py`.
- Enumerations are `StrEnum`. Literals with business meaning live in one place (`kinds.py`, `severity.py`, `CaseStatus`).

**Control flow**
- Return early. No `else` after `return`. Nesting depth ≤ 3.
- Model expected outcomes as typed results (`TriageOutcome`, `ExecResult`). Exceptions are for exceptional conditions (01 §6).
- `match` statements over discriminated unions must be exhaustive (`assert_never` in the default branch).

**Comments and docs**
- Docstrings say *what* and *why*, and cite the spec (`"""Fail-closed calibration (ARCH §4.2, ADR-0002)."""`). Comments explain non-obvious *why*, never restate code.
- No commented-out code. No `TODO` without an issue link.

**Configuration and globals**
- No module-level mutable state, no singletons, and no `os.environ` reads outside `config/loader.py`.
- Everything a service needs arrives through its constructor (dependency injection). `bootstrap.build_container()` is the only place that constructs adapters.

**Logging**
- `structlog`, JSON output, always bound with `tenant_id` and `trace_id`. Never log payload contents, ticket bodies, prompts or secrets: log ids, counts and hashes. Events (INV-03) are the audit record; logs are for operators.

---

## 4. Design patterns: where and why

| Pattern | Where | Why it's the right tool here |
|---|---|---|
| **Ports and Adapters (hexagonal)** | `ports/` + `adapters/` | Invariant logic is testable with fakes; ServiceNow, LLM providers and DB engines are swappable |
| **Composition root / constructor DI** | `bootstrap.py` | One place wires concrete adapters; tests override any port |
| **Repository** | `ports/store.py`, `adapters/db/repos/` | Intention-revealing persistence (`upsert_by_fingerprint`), no SQL in use cases |
| **Unit of Work** | `SqlUnitOfWork` | One transaction per use case step: the proposal state change and the outbox insert commit together (single-use approvals depend on it) |
| **Transactional Outbox** | `outbox` table + `OutboxExecutor` | External effects happen after commit, are retried idempotently, and are gated by DB triggers (INV-01, INV-04) |
| **Command** | `ActionSpec` → `Proposal` → `OutboxRow` | An action is data that can be hashed, approved, persisted, replayed and audited before it runs |
| **State machine (explicit transition tables)** | `domain/proposal_state.py`, `domain/lifecycle.py` | Illegal transitions are impossible by construction and property-testable; repositories persist with CAS on the expected state |
| **Strategy** | `LLMProvider` backends, `SecretDetector` classes, per-adapter field mapping, change-feed sources | Interchangeable algorithms behind one interface; selected by config in the composition root |
| **Decorator** | `TracingLLMProvider`, `FaultInjectingLLMProvider`, `FaultInjectingTicketSink`, `RetryingTicketSink`, `CircuitBreakerTicketSink` | Cross-cutting concerns (tracing, fault injection, retries, breakers) wrap ports without touching business code. `LLMClient` composes them in a fixed order: breaker → retry → tracing → provider |
| **Template Method (fixed pipeline inside one function)** | `LLMClient.structured()`: render → redact → send → validate → record | The redaction step can't be skipped or reordered by a caller (INV-06) |
| **Pipeline of stages** | `TriageService.handle` | Each stage returns a typed result or a `SafeDefaulted` outcome, which keeps Loop A linear (ARCH §4) |
| **Registry** | `ActionKind` metadata, tool registry, prompt registry | Lookup by name with load-time validation (tier/class agreement, prompt schema existence) |
| **Specification** | CEL `check` expressions compiled to `CompiledCheck.is_satisfied_by(result)` (v0.1) | Evidence rules are data, compiled and validated in CI, never code from a runbook |
| **Factory method** | `bootstrap.make_llm_provider(cfg)`, `make_ticket_adapter(cfg)` | Config → concrete strategy, in one place |
| **Builder (tests only)** | `tests/builders.py` | Readable tests with only the relevant fields |
| **Null Object** | `NoopPagingIntegration` in v0.0 | Removes `if paging:` branches; `doctor` still requires a real one before triage |

**Patterns we deliberately avoid:** service locators, global event buses (events are written through `EventSink` inside the UoW, not broadcast), ORM active record (`SQLAlchemy Core` only), inheritance hierarchies deeper than one level (prefer composition and Protocols), and metaclass or decorator magic for business rules.

---

## 5. SOLID, applied

- **Single responsibility:** a service owns one use case group, and an adapter one external system.
- **Open/closed:** a new LLM provider, detector or tool is a new class registered in the composition root. No existing use case changes.
- **Liskov:** every implementation passes its port's contract suite (04 §4). That is the substitutability check.
- **Interface segregation:** `TicketSource` and `TicketSink` are separate, so the shadow runner gets a source and **no sink**. Shadow mode therefore cannot write by construction.
- **Dependency inversion:** `app/` depends on `ports/` only (import-linter `app-no-adapters`).

---

## 6. Security practices

- Treat every inbound text field as untrusted: wrap it in prompts (02 §5), never interpolate it into SQL (bound parameters only, enforced by ruff `S608`), and never use it as an action target (catalog validation).
- Secrets come from the environment through `config/loader.py`, held in `SecretStr`, and are never logged or put in events.
- Webhooks: constant-time HMAC compare (`hmac.compare_digest`), a 5-minute timestamp window, and a nonce cache in the DB.
- Admin UI: secure, HttpOnly, SameSite=Lax session cookie, CSRF on every POST, and a strict `Content-Security-Policy` (`default-src 'self'`).
- Dependencies are pinned in `uv.lock`; `pip-audit` runs in CI and a high-severity finding blocks merge.

---

## 7. Git and PR conventions

- Trunk-based development: short-lived branches named `wp-011-loop-a`; squash-merge to `main`; `main` is always releasable.
- Conventional commits (`feat`, `fix`, `test`, `refactor`, `docs`, `chore`, `build`, `ci`) with a scope, and the WP id in the body (`Refs: WP-011`).
- One WP per PR where practical. A PR larger than about 600 changed lines of production code is split.
- The PR template contains: WP id, spec sections implemented, test IDs added, the DoD checklist (tech-spec README), screenshots for UI, and the ADR link if the spec changed.

---

## 8. Review checklist

- [ ] The tests named in the WP exist with the exact IDs, and they were committed red first.
- [ ] No layer violations; no new dependency without an ADR.
- [ ] Every new external effect goes through the outbox. No direct `TicketSink` call from a use case except inside `OutboxExecutor`.
- [ ] Every new LLM call goes through `StructuredLLM.structured()`.
- [ ] No `datetime.now()`, `uuid4()` or `random` outside the clock and id adapters.
- [ ] Error handling follows 01 §6: no silent catch, and vendor exceptions are translated.
- [ ] New enums, kinds or event kinds were added to their single source of truth, the DB CHECK constraints were regenerated, and the docs-kind lint passes.
- [ ] Names come from the spec vocabulary, and functions are small and single-purpose.
- [ ] Logs contain no payloads or secrets.
- [ ] Spec or ADR updated if behaviour deviates.
