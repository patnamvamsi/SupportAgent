# 01. Architecture and Contracts

Implements: ARCH §1–§12, §15, §18. Package layout per [ADR-0001](../adr/0001-python-package-layout.md).

---

## 1. Package layout

The style is hexagonal (ports and adapters). The domain is pure, use cases depend only on ports, adapters implement ports, and one composition root wires everything together.

```
supportagent/                         repository root
├── pyproject.toml                    uv-managed; one distribution: supportagent
├── supportagent.example.yaml         reference config (02 §1)
├── src/supportagent/
│   ├── domain/                       PURE: no I/O, no clocks, no randomness, no frameworks
│   │   ├── ids.py  hashing.py  errors.py  severity.py  tiers.py
│   │   ├── kinds.py                  ActionKind + KIND_META, CUSTOMER_FACING_KINDS, NEVER_EXECUTABLE_KINDS (ARCH §12.2)
│   │   ├── events.py                 EventKind StrEnum (ARCH §15)
│   │   ├── trace.py                  TraceContext
│   │   ├── model/                    frozen pydantic models (§4): alert, case, proposal, grant, comms,
│   │   │                             event, triage, rca, catalog, taxonomy, user
│   │   ├── normalize.py  fingerprint.py  idempotency.py               (§7.2–§7.5)
│   │   ├── preclass.py               RuleSet.classify()
│   │   ├── classification.py         prefix_match(), calibrated_score() (§7.7)
│   │   ├── routing.py                RoutingTable.lookup()
│   │   ├── stats.py                  wilson_lower_bound() (§7.6)
│   │   ├── proposal_state.py  lifecycle.py                           (§7.10, §7.11)
│   │   ├── takeover.py  comms.py  cadence.py  grouping.py  rbac.py  rca_reduce.py      [v0.1]
│   ├── ports/                        Protocols only. No implementations.
│   │   ├── clock.py  idgen.py  llm.py  secrets.py  tickets.py  inbound.py  store.py  events.py
│   │   ├── identity.py  paging.py  tools.py  checks.py (CheckCompiler, CompiledCheck)  [tools/checks/paging: v0.1]
│   ├── app/                          use cases; depend on domain + ports only
│   │   ├── triage.py  classify.py  propose.py  gate.py  outbox.py  handlers.py  sync.py
│   │   ├── setup.py  shadow.py  stats.py  replay.py  doctor/ (checks/*.py)
│   │   ├── lifecycle.py  comms.py  memory.py  feedback.py  rca.py  grouping.py  spend.py   [v0.1]
│   ├── adapters/                     each subpackage is independent of the others
│   │   ├── db/                       SQLAlchemy Core: engine.py, tables.py, uow.py, errors.py, repos/*.py
│   │   ├── crypto/aead.py            AES-GCM envelope (03 §3)
│   │   ├── system/                   SystemClock, UlidIdGenerator
│   │   ├── files/                    catalog, taxonomy, calibration, runbook loaders
│   │   ├── llm/                      client.py (LLMClient), redact.py, detectors/*, prompts.py,
│   │   │                             providers/{anthropic,openai,ollama,fixture}.py, faults.py (the llm event: LLMClient)
│   │   ├── servicenow/               http.py, auth.py, source.py, sink.py, mapping.py
│   │   ├── identity/oidc.py
│   │   ├── resilience/               retry.py, breaker.py (Decorators over ports)        [v0.1]
│   │   ├── tools/                    logs, airflow, gha, cron, oncall, change_feed/*     [v0.1]
│   │   ├── cel/check.py              CheckCompiler over celpy (Specification)            [v0.1]
│   │   ├── paging/                   pagerduty.py, opsgenie.py, noop.py                  [v0.1; noop v0.0]
│   │   ├── webhook/verify.py                                                             [v0.1]
│   │   └── fakes/                    FakeServiceNow(+HTTP), FakeLLMProvider, FakeOIDC, FakeTool, faults
│   ├── config/                       schema.py (pydantic models of supportagent.yaml), loader.py
│   ├── evals/                        harness.py, scoring.py, snapshot.py, calibration.py
│   ├── entrypoints/
│   │   ├── api/                      app.py, deps.py, csrf.py, routes/*.py, templates/*.html, static/
│   │   ├── cli/                      main.py (Typer) + one module per command
│   │   └── worker/                   scheduler.py, leases.py, jobs.py
│   └── bootstrap.py                  composition root: build_container(config) -> Container
├── migrations/                       Alembic env + versions/ (03 §4)
├── prompts/<slot>/v<N>.md            versioned prompt files (02 §5)
├── taxonomy/classification.yaml  taxonomy/hypotheses.yaml
├── catalog/services.yaml             service catalog (customer-provided; example shipped)
├── runbooks/*.yaml  runbooks/*.md    [v0.1]
├── evals/cases/  evals/snapshots/<id>/  evals/secrets_heldout/  evals/artifacts/
├── tests/                            mirrors src/ (04 §2)
└── docker/Dockerfile  docker/compose.yaml
```

The root data directories (`prompts/`, `taxonomy/`, `catalog/`, `runbooks/`, `evals/`) are **data, not code**. The loader validates them at startup and in CI.

---

## 2. Layers and dependency rules

```
entrypoints ──► bootstrap ──► adapters ──► ports ◄── app ──► domain
     │                                       │                ▲
     └──────────────► app ───────────────────┴────────────────┘
```

Enforced by **import-linter** in CI (`.importlinter`):

| Contract | Rule |
|---|---|
| `domain-pure` | `supportagent.domain` imports only stdlib, `pydantic`, `typing_extensions` and itself. Forbidden: `sqlalchemy`, `httpx`, `fastapi`, `jinja2`, `openai`, `anthropic`, `celpy`, `structlog`, `cryptography`; `os.environ` and `datetime.now` are banned by a ruff rule scoped to `domain/`. |
| `ports-protocol-only` | `supportagent.ports` imports only `domain` and `typing`. It contains no classes with method bodies other than `...`. |
| `app-no-adapters` | `supportagent.app` must not import `supportagent.adapters`, `supportagent.entrypoints` or `supportagent.bootstrap`. |
| `adapters-independent` | No `supportagent.adapters.<x>` subpackage imports another `supportagent.adapters.<y>` (import-linter `independence` contract over all adapter subpackages). Shared code goes to `domain` or `ports`. `adapters.fakes` may import ports and domain only. |
| `only-bootstrap-wires` | Only `bootstrap` and `entrypoints` import `adapters`. |
| `entrypoints-thin` | `entrypoints` call `app` services. They never execute SQL or HTTP directly. |

**Why:** INV-01..06 are application rules, and they must be testable with fakes in milliseconds. The ports make that possible. A use case that needs a database row asks a repository protocol, never SQLAlchemy.

---

## 3. Ports (exact signatures)

All ports are `typing.Protocol` classes. Implementations are checked with `mypy --strict`, and contract tests run against every implementation ([04 §4](04-test-strategy.md#4-contract-tests)).

```python
# ports/clock.py
class Clock(Protocol):
    def now(self) -> datetime: ...            # timezone-aware UTC, always

# ports/idgen.py
class IdGenerator(Protocol):
    def new(self, prefix: IdPrefix) -> str: ...   # "<prefix>_<ULID>", e.g. "case_01J9Z..."

# ports/secrets.py
@dataclass(frozen=True)
class SecretSpan:
    start: int; end: int; detector: DetectorClass   # DetectorClass is a StrEnum (§7.13)
class SecretDetector(Protocol):
    detector_class: DetectorClass
    def find(self, text: str) -> Sequence[SecretSpan]: ...

# ports/llm.py
@dataclass(frozen=True)
class ProviderRequest:
    model_id: str                 # pinned dated snapshot id (ARCH §9)
    system: str
    user: str                     # already redacted; providers never see raw text
    json_schema: Mapping[str, Any]
    max_output_tokens: int
    timeout_s: float
@dataclass(frozen=True)
class ProviderResponse:
    raw_text: str
    resolved_model: str           # what the provider reports it actually used
    tokens_in: int; tokens_out: int; latency_ms: int
class LLMProvider(Protocol):
    name: str
    def complete_json(self, req: ProviderRequest) -> ProviderResponse: ...
        # raises LLMUnavailable | LLMTimeout | LLMRateLimited  (§6)

class StructuredLLM(Protocol):    # implemented by adapters/llm/client.py:LLMClient
    def structured(self, slot: SlotName, variables: Mapping[str, str],
                   schema: type[TModel], *, trace: TraceContext,
                   timeout_s: float | None = None) -> TModel | None: ...
        # renders prompts/<slot>/v<N>.md → REDACT (mandatory) → provider → validate.
        # Returns None on any failure (timeout, schema-invalid, provider error); never raises to callers.
        # Emits llm + redact events through EventSink in every case.

# ports/tickets.py          [WP-M07, ADR-0013: the connector contract]
class RecordType(StrEnum): INCIDENT = "incident"; ALERT = "em_alert"   # values = stored keys
class NoteVisibility(StrEnum): INTERNAL = "internal"; CUSTOMER = "customer"
@dataclass(frozen=True)
class PollBatch:
    records: Sequence[TicketRecord]   # current state of each changed record
    next_cursor: datetime             # max(updated_at) seen; caller persists it
class TicketSource(Protocol):
    def poll(self, record_type: RecordType, since: datetime, limit: int, offset: int = 0) -> PollBatch: ...
    def record(self, record_type: RecordType, external_id: str) -> TicketRecord | None: ...
    def history(self, record_type: RecordType, external_id: str,
                after_mod_count: int, since: datetime | None = None) -> Sequence[ChangeRecord]: ...
        # field + note changes, ordered; only when the manifest declares `history`
class TicketSink(Protocol):
    def append_journal(self, record_type: RecordType, external_id: str,
                       visibility: NoteVisibility, text: str) -> ExternalRef: ...
    def update_fields(self, record_type: RecordType, external_id: str,
                      fields: Mapping[str, str]) -> ExternalRef: ...
    def find_marker(self, record_type: RecordType, external_id: str, marker: str) -> ExternalRef | None: ...
        # reconciliation lookup (§7.5)
class Connector(TicketSource, TicketSink, Protocol):
    manifest: ConnectorManifest       # domain.connector: capabilities -> limits_for()
    semantics: TicketSemantics        # state/assignee/priority fields, resolved states, severity

# ports/tools.py            [v0.1]
class Tool(Protocol):
    name: str                       # "logs.grep", "gha.recent_runs", ...
    trust_class: TrustClass
    tier: Tier
    result_schema: Mapping[str, Any]    # JSON Schema; CEL checks compile against it
    def run(self, args: Mapping[str, Any], *, trace: TraceContext,
            timeout_s: float) -> ToolResult: ...   # raises ToolError (§6)

# ports/inbound.py
class InboundHandler(Protocol):          # SyncService → TriageService/LifecycleService, wired in bootstrap
    def on_alert(self, evt: AlertEvent) -> None: ...
    def on_changes(self, case_ref: ExternalCaseRef, changes: Sequence[ChangeRecord]) -> None: ...

# ports/checks.py            [v0.1]
class CompiledCheck(Protocol):           # Specification pattern
    def is_satisfied_by(self, result: Mapping[str, Any], service: ServiceEntry,
                        case: Mapping[str, Any]) -> tuple[bool, str]: ...   # (ok, why)
class CheckCompiler(Protocol):
    def compile(self, expr: str, result_schema: Mapping[str, Any]) -> CompiledCheck: ...
        # raises ConfigError if the expression references unknown fields or isn't valid CEL

# ports/events.py
class EventSink(Protocol):
    def append(self, event: Event) -> None: ...   # inside the current UoW transaction

# ports/store.py
class UnitOfWork(Protocol):
    cases: CaseRepository; alerts: AlertRepository
    proposals: ProposalRepository; approvals: ApprovalRepository
    grants: GrantRepository; outbox: OutboxRepository
    comms_plans: CommsPlanRepository; comms: (written by the cadence job, status by the outbox executor, read by AdminQueries.case; BL-004)
    events: EventRepository; users: UserRepository
    sync: SyncRepository; leases: LeaseRepository
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc: object) -> None: ...   # rollback on exception
    def commit(self) -> None: ...
# Repositories expose intention-revealing methods only, for example:
class CaseRepository(Protocol):
    def upsert_by_fingerprint(self, *, case_id: str, tenant_id: str, fingerprint: str,
                              now: datetime) -> UpsertResult: ...   # (§7.3, ARCH §4.1)
    def get(self, case_id: str) -> Case: ...
    def set_triage(self, case_id: str, triage: TriageDecision) -> None: ...
    def transition(self, case_id: str, to: CaseStatus, *, expected: CaseStatus) -> None: ...  # CAS
class OutboxRepository(Protocol):
    def insert(self, row: NewOutboxRow) -> OutboxRow: ...   # raises AuthorizationDenied on trigger ABORT
    def claim_next(self, *, now: datetime, lease_s: int) -> OutboxRow | None: ...  # BEGIN IMMEDIATE + CAS
    def mark_done(self, outbox_id: str, ref: ExternalRef, now: datetime) -> None: ...
    def mark_failed(self, outbox_id: str, error: str, retry_at: datetime | None) -> None: ...

# ports/identity.py
class IdentityProvider(Protocol):
    def authorization_url(self, state: str, nonce: str) -> str: ...
    def exchange(self, code: str, nonce: str) -> VerifiedIdentity: ...   # sub, email, name

# ports/paging.py           [v0.1]
class PagingIntegration(Protocol):
    def find_open_page(self, service: str, source_ref: str) -> PageRef | None: ...
    def add_note(self, page: PageRef, text: str) -> ExternalRef: ...
```

**Rule:** no port returns vendor types (httpx responses, SQLAlchemy rows, SDK objects). Adapters translate at the boundary.

---

## 4. Domain types

All models are `pydantic.BaseModel` with `model_config = ConfigDict(frozen=True, extra="forbid", strict=True)`. All datetimes are timezone-aware UTC; a naive datetime is a validation error.

```python
class Severity(StrEnum): P1 = "P1"; P2 = "P2"; P3 = "P3"; P4 = "P4"
class Tier(StrEnum): T0 = "T0"; T1 = "T1"; T2 = "T2"; T3 = "T3"
class TrustClass(StrEnum): READ = "read"; EXECUTE = "execute"; STATE_CHANGING = "state-changing"
class CaseStatus(StrEnum):
    OPEN="open"; TRIAGED="triaged"; IN_PROGRESS="in_progress"
    HUMAN_OWNED="human_owned"; RESOLVED="resolved"; REOPENED="reopened"
class ProposalStatus(StrEnum):
    PROPOSED="proposed"; PENDING_APPROVAL="pending_approval"; APPROVED="approved"
    REJECTED="rejected"; EXPIRED="expired"; EXECUTED="executed"; SUPERSEDED="superseded"

class AlertEvent(BaseModel):                 # canonical inbound event (ROADMAP canonical types)
    tenant_id: str
    source: Literal["servicenow", "webhook"]
    origin: Literal["alert", "ticket"]       # alert-derived vs human-filed (fingerprint rule, §7.3)
    external_id: str                         # ServiceNow sys_id or webhook event id
    table: str | None                        # "incident" | "em_alert" | None
    ticket_number: str | None                # required when origin == "ticket"
    received_at: datetime
    subject: constr(max_length=4000)
    body: constr(max_length=65536)           # adapter truncates with "\n[truncated]"
    resource: str | None
    alert_type: str | None
    error_signature: str | None              # raw; normalized inside fingerprint()
    severity_hint: Severity | None
    cmdb_ci: str | None
    raw_ref: str                             # pointer to encrypted raw payload (alerts.payload_ref)

class TriageResult(BaseModel):               # the LLM slot schema for "classify"
    classification: constr(pattern=r"^[a-z0-9_]+/[a-z0-9_]+$")
    severity: Severity
    summary: constr(max_length=600)
    confidence: confloat(ge=0, le=1)         # logged only; never gates (ARCH §4.2)

class TriageDecision(BaseModel):             # what Loop A decided, persisted on the case
    outcome: Literal["routed", "safe_default"]
    reason: Literal["llm-failure", "classifier-conflict", "low-calibrated-score",
                    "unconfirmed-high-severity", "unresolved-service",
                    "no-paging-integration",   # ADR-0006 (ARCH §14)
                    "storm-member",            # ADR-0007 (ARCH §4.3)
                    "spend-breaker",
                    "class-not-ready",         # ADR-0015 §5 (WP-O6b): no ready class has the label
                    "routed"]
    classification: str | None
    severity: Severity | None
    preclass_label: str | None
    calibrated_score: float | None
    route_id: str | None
    llm_suggestion: TriageResult | None

class ActionSpec(BaseModel):                 # Command object: one proposed action
    kind: ActionKind
    target: TargetRef                        # resolved via service catalog; never free text
    scope: constr(min_length=1)              # literal scope string (ARCH §6.4)
    payload: dict[str, JsonValue]            # floats forbidden (§7.1)

class TargetRef(BaseModel):
    service: str                             # catalog services.name
    system: Literal["servicenow", "airflow", "gha", "pager", "email"]
    ref: str                                 # e.g. "incident:<sys_id>", "dag:nightly-sync"

class Event(BaseModel):
    id: str; tenant_id: str; trace_id: str; span_id: str; parent_span_id: str | None
    kind: EventKind                          # StrEnum; canonical list ARCH §15
    actor: str                               # "agent" | "user:<id>" | "system"
    model: str | None = None
    tokens_in: int = 0; tokens_out: int = 0; latency_ms: int = 0
    status: Literal["ok", "error", "skipped"] = "ok"
    payload: dict[str, JsonValue]            # must contain "v": <int> payload schema version
    ts: datetime

class ServiceEntry(BaseModel):               # catalog/services.yaml row (ARCH §7)
    name: constr(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    repos: list[str] = []; log_paths: list[str] = []; dags: list[str] = []
    cmdb_ci: list[str] = []; resources: list[str] = []   # alert `resource` values that map here
    oncall_schedule: str | None = None; owners: list[str] = []; depends_on: list[str] = []
    paging_integration: str | None = None    # required before triage (ARCH §14); doctor checks
```

`TraceContext` is a frozen dataclass `(tenant_id, trace_id, span_id)` with a `child()` method that returns a new span id. It is passed explicitly, never stored in globals or context variables.

---

## 5. Application services (use cases)

Each service is a class constructed with its ports (constructor injection). It has one public method per use case and no static state.

| Service | Public methods | Invariants it relies on |
|---|---|---|
| `TriageService` | `handle(evt: AlertEvent) -> TriageOutcome` | INV-02, INV-05, INV-03 |
| `GateService` | `submit(case_id, spec: ActionSpec, trace) -> Proposal`; `approve(proposal_id, approver_id, seen_hash, reason) -> Approval`; `reject(...)`; `expire_due(now) -> int` | INV-01 |
| `OutboxExecutor` | `enqueue(authorization, spec, case_id, trace) -> OutboxRow`; `run_once(now) -> ExecResult` | INV-01, INV-04 |
| `SyncService` | `poll_table(table) -> SyncReport` | ARCH §8.1 |
| `SetupService` | `request_default_grants(admin_id) -> list[Grant]`; `approve_grants(ids, admin_id)` | ARCH §6.4 |
| `ShadowRunner` | `run(evt) -> ShadowRecord` | no side effects |
| `ReplayService` | `replay(trace_id) -> ReplayView` | INV-03 |
| `StatsService` | `report(window, mode) -> StatsReport` | mode-separated (ARCH §8.4) |

`TriageOutcome` is a discriminated union: `Linked(case_id)` | `Retriaged(case_id, decision)` | `Triaged(case_id, decision, proposals)` | `SafeDefaulted(case_id, decision)`. Expected business outcomes are **values**, not exceptions (§6).

---

## 6. Error taxonomy

```
SupportAgentError                      base; never raised directly
├── ConfigError                        invalid supportagent.yaml / catalog / taxonomy (startup fails)
├── DomainValidationError              a domain rule was violated by input (e.g. float in payload)
├── AuthorizationDenied                INV-01 trigger ABORT, self-approval, RBAC denial
│     .code: Literal["no_valid_authorization", "self_approval", "immutable", "rbac"]
├── ConcurrencyConflict                CAS failed (expected status mismatch); caller may retry
├── NotFound
├── ExternalServiceError               adapters only; .retryable: bool, .retry_after_s: float | None
│   ├── ServiceNowError
│   ├── LLMUnavailable / LLMTimeout / LLMRateLimited
│   └── ToolError
└── BudgetExhausted                    RCA/LLM budget; caught inside the use case
```

Rules:
1. Adapters translate every vendor exception into this taxonomy. The DB adapter maps an IntegrityError whose message starts with `INV-01:` to `AuthorizationDenied`, with `.code` taken from the message suffix.
2. Use cases **catch only what they can handle**, and convert it to a typed outcome (for example LLM failure → `SafeDefaulted(reason="llm-failure")`). Everything else propagates to the entrypoint, which logs it with the trace id and returns a safe response.
3. `except Exception` is allowed only in three places, each of which emits an `error` event: the worker job loop, the API exception handler and the CLI top level.
4. Never swallow an exception silently. Every catch either re-raises, returns a typed outcome, or emits an event.

---

## 7. Algorithms (normative detail)

### 7.1 Canonical JSON and hashing

```python
def canonical_json(value: JsonValue) -> str:
    _reject_floats(value)                    # DomainValidationError: use int or str for numbers
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def payload_hash(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()   # 64 lowercase hex
```

Floats are forbidden in hashed payloads because their serialization isn't canonical across languages. Strings are NFC-normalized before hashing (`unicodedata.normalize("NFC", s)`, applied recursively). The `plan_hash` of a comms plan is `payload_hash({"audience":…, "channel":…, "template_id":…, "allowed_fields":…, "cadence_minutes":…})`.

### 7.2 `normalize(text, tenant_constants)`

Applied in this order:

| Step | Pattern (Python `re`, IGNORECASE) | Replacement |
|---|---|---|
| 1 | `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` | `<uuid>` |
| 2 | ISO-8601 and common log timestamps: `\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?` | `<ts>` |
| 3 | `\b\d{1,3}(\.\d{1,3}){3}(:\d{1,5})?\b` | `<ip>` |
| 4 | `\b0x[0-9a-f]+\b` or `\b[0-9a-f]{8,}\b` | `<hex>` |
| 5 | each tenant constant (literal, escaped), longest first | `<const>` |
| 6 | `\b\d{3,}\b` | `<n>` |
| 7 | collapse whitespace to one space, strip, lowercase, truncate to 512 chars | |

Property tests: idempotent (`normalize(normalize(x)) == normalize(x)`), and insensitive to UUID, timestamp and IP substitutions.

### 7.3 `fingerprint(evt)`

```python
def fingerprint(evt: AlertEvent, consts: Sequence[str]) -> str:
    if evt.origin == "ticket":
        parts = ["ticket", evt.source, _req(evt.ticket_number)]
    else:
        parts = ["alert", evt.source, _n(evt.resource), _n(evt.alert_type),
                 _n(evt.error_signature)]          # _n = normalize or "" when None
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:40]
```

`upsert_by_fingerprint` executes the statement in ARCH §4.1 and returns `UpsertResult(case_id, created: bool, duplicate_count: int)`. Severity escalation (the only material change) means `evt.severity_hint` ranks above the case's current severity (`P1 > P2 > P3 > P4`).

### 7.4 Idempotency keys

| Authorization path | Key |
|---|---|
| approval | `sha256("appr|" + approval_id)` |
| grant | `sha256("grant|" + case_id + "|" + action_kind + "|" + scope + "|" + payload_hash)` |
| comms plan | `sha256("plan|" + plan_id + "|" + audience + "|" + trigger + "|" + str(interval_slot))` |

All keys are the first 32 hex characters. A grant-path action with the same case, kind, scope and payload therefore runs once per case. Re-posting an identical work note is intentionally a no-op.

### 7.5 Reconciliation markers

- **ServiceNow journal** (`work_notes`, `comments`): the last line of the text is `[sa:<idempotency_key>]`. `find_marker` queries `sys_journal_field` with `element_id=<sys_id>^valueLIKE[sa:<key>]`.
- **ServiceNow field update:** before writing, read the field values. If they already equal the target values, it's a no-op returning the existing ref. Field updates are naturally idempotent, so no marker is needed.
- **Email:** header `X-SupportAgent-Key: <key>` plus a local `comms` row check. Email cannot be read back, so its reconciliation is local-only. This is a documented limitation, and the soak measures it.

### 7.6 Wilson lower bound

```python
def wilson_lower_bound(successes: int, n: int, z: float = 1.959963984540054) -> float:
    if n == 0: return 0.0
    p = successes / n
    denom = 1 + z*z/n
    centre = p + z*z/(2*n)
    margin = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))
    return (centre - margin) / denom
```

Golden tests: `(35, 35) ≥ 0.900`, `(49, 50) ≈ 0.895`, `(24, 30) ≈ 0.627`, `(73, 73) ≥ 0.950`.

### 7.7 `calibrated_score(label, preclass_agrees)`

Reads `evals/artifacts/calibration.json`, which the quality suite produces (WP-105). A `holdout` table must carry `report_id` and `signature` (sha256 over version, grade, snapshot, model, report id and counts); the loader rejects a missing or mismatched signature, so hand-edited counts never load. This is a checksum against edits, not a cryptographic signature:

```json
{"version": 3, "grade": "holdout", "snapshot_id": "snap-2026-11-02", "model_id": "...",
 "labels": {"database/saturation": {"agree": {"n": 41, "correct": 39},
                                     "abstain": {"n": 18, "correct": 15}}},
 "report_id": "20261102T0900Z-quality-live",
 "signature": "<sha256 of the counts + report_id>"}
```

`score = wilson_lower_bound(correct, n)` for the row that applies (`agree` if the pre-classifier agrees, `abstain` if it abstained). The score is **0.0** if the label is missing, if that row is missing, or if `n < 10` (prov.). This is the fail-closed rule in ARCH §4.2, made precise in [ADR-0002](../adr/0002-calibrated-score-definition.md).

**Grades.** A table has `grade: smoke` (produced by the v0.0 smoke suite, WP-016) or `grade: holdout` (produced by the quality suite, WP-105). Config `calibration.min_grade` is `smoke` in v0.0 and `holdout` from v0.1. A table below the configured grade is rejected at load, and every label then scores 0. The v0.0 smoke suite therefore contains **≥ 12 cases of each label in v0.0's routing table** (v0.0 ships one routing row, CAP §5), so that label can route. Every other label safe-defaults, which is the intended v0.0 behaviour.

### 7.8 Storm grouping (v0.1)

Given a new case `c` with service `s` and source `src` at time `t`:
1. Candidate groups: open groups for the tenant with `opened_ts ≥ t − 10 min` (prov.).
2. Join the first candidate (oldest first) where any member shares `s`, or where `s` and a member's service are related by `depends_on` (either direction), or where the group contains ≥ 5 (prov.) cases with source `src`.
3. Otherwise open a new group if the tenant has > 20 (prov.) new cases in the last 5 minutes (storm mode). If not, leave the case ungrouped.
4. A group closes when all its members are resolved, or after 24 h without new members.

This is a pure function, `assign_group(case, open_groups, recent_counts, catalog) -> GroupDecision`, and the I/O around it lives in `app/grouping.py`.

### 7.9 Severity cost matrix

Cost of predicting a severity when the truth was another (rows are truth, columns are prediction):

| | P1 | P2 | P3 | P4 |
|---|---|---|---|---|
| **P1** | 0 | 3 | 8 | 10 |
| **P2** | 1 | 0 | 3 | 6 |
| **P3** | 1 | 1 | 0 | 2 |
| **P4** | 2 | 1 | 1 | 0 |

These values are provisional and versioned in `evals/cost_matrix.yaml`.

### 7.10 Proposal state machine

Implemented as a pure function, `next_status(current, event) -> ProposalStatus`, which raises `DomainValidationError` on an illegal transition.

| From | Event | To |
|---|---|---|
| proposed | `submitted` | pending_approval |
| proposed | `grant_matched` | approved (grant path; no approval row) |
| pending_approval | `approved` | approved |
| pending_approval | `rejected` | rejected |
| pending_approval | `ttl_elapsed` | expired |
| pending_approval / approved | `case_retriaged` | superseded |
| approved | `outbox_inserted` | executed (set by trigger `trg_outbox_mark_executed`) |

**Conditioned grants (ADR-0014 part 2).** `grant_matched` needs more than a covering grant in two cases. If the grant has `conditions_json`, or the scope is an SOP step's (`sop:…`), the gate evaluates the conditions against the evidence the caller passes: `rca.run` passes the RCA's positive checks and whether the cause's claim passed `entail`. The rules:
- An `sop:` grant without conditions covers nothing.
- A condition that is unreadable, unknown or can't be evaluated fails closed, and so does a confidence condition before ADR-0003 calibration. The proposal goes to `submitted`.
- Only kinds in `domain.auto.REVERTS` qualify.
- Each evaluation writes a `gate` event with an `auto` record: grant, conditions, evidence, the daily count, refusals. The transition is `grant_enqueued` or `auto_declined`.
- The DB trigger (path 2) is unchanged: conditions only narrow what a grant covers.

### 7.11 Case lifecycle

| From | Event | To |
|---|---|---|
| open | `triaged` | triaged |
| open | `safe_defaulted` | triaged (with `outcome=safe_default`) |
| triaged | `work_started` (first executed action or human work note) | in_progress |
| open / triaged / in_progress / reopened | `human_takeover` | human_owned |
| human_owned | `hand_back` | in_progress |
| any non-resolved | `resolved` (ServiceNow state resolved/closed) | resolved |
| resolved | `reopened` (ServiceNow state reopened) | reopened |
| reopened | `triaged` | triaged |

### 7.12 Cadence slot (v0.1)

`interval_slot = floor((now − plan.approved_at) / cadence_minutes)`. The first update has slot 0. A missed slot is not back-filled: only the current slot is sent.

### 7.13 Redaction detector classes (v0.0)

`email`, `aws_access_key`, `generic_api_key` (key=value with high entropy ≥ 3.5 bits/char and length ≥ 20), `bearer_token`, `jwt`, `private_key_block`, `connection_string` (scheme://user:pass@host), `card_number` (Luhn-valid 13–19 digits), `phone_e164`, plus tenant `custom_patterns`. Each match is replaced by `<CLASS_n>`, numbered per request. The placeholder map lives only in memory for that request.
