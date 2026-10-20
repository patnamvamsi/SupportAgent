"""Global test options (04 §3-§6).

Sockets are disabled by default (``--disable-socket`` in the pyproject
``addopts``): unit/property/contract suites run with **no network and no
Docker** (04 §1). Suites that need a live endpoint opt in per run
(``make record-sn`` via ``--allow-hosts``; live-LLM evals via ``--live-llm``).

The fixture table in 04 §3 (``clock``, ``ids``, ``db``, ``uow``, ``events``,
``fake_sn``, ``fake_llm``, ``fake_oidc``) lands with the WPs that own it.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from supportagent.adapters.fakes.clock import FrozenClock
from supportagent.adapters.fakes.ids import SequentialIdGenerator

_TESTS_ROOT = Path(__file__).resolve().parent
# Top-level test directory → 04 §1 marker, so ``pytest -m "unit or property"``
# selects each suite without per-function annotations.
_LAYER_BY_DIR: dict[str, str] = {
    "unit": "unit",
    "property": "property",
    "contract": "contract",
    "invariants": "invariant",
    "integration": "integration",
    "e2e": "e2e",
    "faults": "faults",
    "soak": "soak",
}
# Markers that count as "belongs to a suite layer" (04 §1). The directory map
# covers all of these except the eval layers, whose tests declare the marker
# explicitly (WP-016).
_LAYER_MARKERS: frozenset[str] = frozenset(_LAYER_BY_DIR.values()) | {
    "eval_smoke",
    "eval_quality",
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    unmarked: list[str] = []
    for item in items:
        try:
            top = item.path.relative_to(_TESTS_ROOT).parts[0]
        except ValueError:
            top = None
        # NB: `add_marker` is unconditional — `item.keywords` also contains
        # enclosing Dir/fixture names (e.g. "unit" for tests/unit/), which
        # are keywords, not markers, and would mask the real test.
        if top is not None and (marker := _LAYER_BY_DIR.get(top)):
            item.add_marker(marker)
        if not any(m.name in _LAYER_MARKERS for m in item.iter_markers()):
            unmarked.append(item.nodeid)
    # 04 §1: every test belongs to exactly one suite layer, and a CI job runs
    # each layer by marker. A test outside a mapped directory with no layer
    # marker of its own (e.g. ``tests/test_x.py``) would be collected but run
    # by no job — fail loudly at collection instead of silently skipping it.
    if unmarked:
        message = (
            "tests without a layer marker (04 §1) — no CI job would run them; "
            "move them into a mapped tests/<layer>/ directory or declare a marker "
            f"from {sorted(_LAYER_MARKERS)}: {unmarked}"
        )
        raise pytest.UsageError(message)


@pytest.fixture
def clock() -> FrozenClock:
    """04 §3: ``FrozenClock(start=2026-10-20T09:00:00Z)`` + ``.advance(seconds)``."""
    return FrozenClock(start=datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC))


@pytest.fixture
def ids() -> SequentialIdGenerator:
    """04 §3: ``SequentialIdGenerator`` → ``case_0001``, ``case_0002``, ..."""
    return SequentialIdGenerator()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--db",
        default="sqlite",
        choices=["sqlite", "postgres"],
        help="database backend for invariant/integration suites (04 §6)",
    )
    parser.addoption(
        "--live-llm",
        action="store_true",
        default=False,
        help="permit live-LLM eval calls (default off; 04 §1)",
    )


# ---------------------------------------------------------------------------
# db / uow / crypto fixtures (04 §3; WP-003)
# ---------------------------------------------------------------------------

# Children-first order so per-test resets respect FKs on both engines
# (04 §3: schema reset per test — TRUNCATE on Postgres, DELETE on SQLite).
# ``events`` (0002, WP-004) has no outgoing FKs — it is a leaf.
# The 10-wp gate tables (0003) reference ``cases``/``users``, so they are
# deleted first, in their own children-first order (outbox → comms_plans →
# approvals → policy_grants → proposals).
_RESET_ORDER: tuple[str, ...] = (
    # 0009 (WP-113): webhook replay protection and inbox.
    "webhook_nonces",
    "webhook_inbox",
    # 0007 (WP-102): comms_drafts references cases and comms_plans.
    "comms_drafts",
    # 0006 (WP-014): shadow_records references cases.
    "shadow_records",
    # 0004 (WP-008): sync state; automation_actors references users.
    "sync_seen",
    "sync_cursors",
    "automation_actors",
    "comms",
    "outbox",
    "comms_plans",
    "approvals",
    "policy_grants",
    "proposals",
    "role_assignments",
    "alerts",
    "events",
    "cases",
    "case_groups",
    "users",
    "blobs",
    "tenant_keys",
    "leases",
)


def _reset_schema(engine: Engine) -> None:
    """04 §3: schema reset per test (Postgres TRUNCATE; SQLite has no TRUNCATE)."""
    if engine.dialect.name == "sqlite":
        # table is drawn from the fixed internal _RESET_ORDER tuple, not input
        statements = [f"DELETE FROM {table}" for table in _RESET_ORDER]  # noqa: S608
    else:
        statements = [f"TRUNCATE {table} RESTART IDENTITY CASCADE" for table in _RESET_ORDER]
    with engine.begin() as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)


@pytest.fixture(scope="session")
def _db_engine(request: pytest.FixtureRequest) -> Iterator[Any]:
    """04 §3: one engine per session — sqlite (tmp file, WAL) or
    testcontainers ``postgres:16`` (one per session) — with migrations applied
    once per session at head."""
    # Deferred (fixture-level) imports keep the sqlite path free of the
    # Postgres-only dependencies (testcontainers) and avoid import-order coupling.
    from supportagent.adapters.db.engine import make_engine  # noqa: PLC0415
    from supportagent.bootstrap import run_migrations  # noqa: PLC0415

    backend = request.config.getoption("--db")
    container: Any = None
    tmpdir: tempfile.TemporaryDirectory[str] | None = None
    if backend == "postgres":
        from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415

        container = PostgresContainer("postgres:16")
        container.start()
        url = container.get_connection_url(driver="psycopg")
    else:
        tmpdir = tempfile.TemporaryDirectory()
        url = f"sqlite:///{tmpdir.name}/invariants.db"
    engine = make_engine(url)
    run_migrations(url)  # 04 §3: applied once per session, at head
    try:
        yield engine
    finally:
        engine.dispose()
        if container is not None:
            container.stop()
        if tmpdir is not None:
            tmpdir.cleanup()


@pytest.fixture
def db(_db_engine: Any) -> Any:
    """04 §3: ``db`` — engine for the selected backend, schema reset per test."""
    _reset_schema(_db_engine)
    return _db_engine


@pytest.fixture
def uow(db: Any) -> Any:
    """04 §3: ``uow`` — ``SqlUnitOfWork(db)``; commit or roll back, never auto."""
    from supportagent.adapters.db.uow import SqlUnitOfWork  # noqa: PLC0415

    return SqlUnitOfWork(db)


@pytest.fixture
def master_key() -> bytes:
    """32-byte AES-256-GCM master key (03 §3); low-entropy fake for gitleaks."""
    return b"test" * 8


@pytest.fixture
def aead(master_key: bytes, uow: Any, clock: FrozenClock) -> Any:
    """03 §3: AES-256-GCM envelope; per-tenant keys wrapped by the master key
    and stored in ``tenant_keys`` via the key-store seam."""
    from supportagent.adapters.crypto.aead import Aead  # noqa: PLC0415
    from supportagent.adapters.db.repos.blobs import SqlKeyStore  # noqa: PLC0415

    return Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock)


@pytest.fixture
def events() -> Any:
    """04 §3: ``events`` — ``RecordingEventSink`` capturing ``EventSink.append``
    into ``.appended`` with a ``.kinds()`` helper (unit level)."""
    from supportagent.adapters.fakes.events import RecordingEventSink  # noqa: PLC0415

    return RecordingEventSink()


@pytest.fixture
def fake_sn() -> Any:
    """04 §3: ``fake_sn`` — in-memory ServiceNow (``TicketSink``) with the
    ``queue_fault`` / ``journal_entries`` / ``fields`` / ``write_count`` seams
    (WP-006, 04 §1)."""
    from supportagent.adapters.fakes.servicenow import FakeServiceNow  # noqa: PLC0415

    return FakeServiceNow()


_TEST_DATA = _TESTS_ROOT / "data"
_REPO_ROOT = _TESTS_ROOT.parent


@pytest.fixture
def fake_llm() -> Any:
    """04 §3: ``fake_llm`` — in-memory ``LLMProvider`` with ``.captured`` (list
    of ``ProviderRequest``) plus ``.respond(json)`` / ``.fail_with(exc)`` seams
    (WP-007, 04 §1)."""
    from supportagent.adapters.fakes.llm import FakeLLMProvider  # noqa: PLC0415

    return FakeLLMProvider()


@pytest.fixture
def canaries() -> dict[Any, str]:
    """One planted secret per detector class, from ``tests/data/canaries/*.txt``
    (INV-06, 01 §7.13)."""
    from supportagent.domain.detectors import DetectorClass  # noqa: PLC0415

    result: dict[Any, str] = {}
    for path in sorted((_TEST_DATA / "canaries").glob("*.txt")):
        result[DetectorClass(path.stem)] = path.read_text(encoding="utf-8").strip()
    return result


@pytest.fixture
def prompt_registry() -> Any:
    """The classify prompt registry rooted at the repo ``prompts/`` dir (02 §5)."""
    from supportagent.adapters.llm.prompts import PromptRegistry  # noqa: PLC0415

    return PromptRegistry(root=_REPO_ROOT / "prompts")


@pytest.fixture
def _build_llm_client(events: Any, clock: Any, ids: Any) -> Any:
    """Wire an ``LLMClient`` over an injected ``provider`` (test seam, 04 §3)."""

    def _build(provider: Any) -> Any:
        from supportagent.adapters.llm.client import LLMClient, SlotSpec  # noqa: PLC0415
        from supportagent.adapters.llm.detectors import built_in_detectors  # noqa: PLC0415
        from supportagent.adapters.llm.prompts import PromptRegistry  # noqa: PLC0415
        from supportagent.adapters.llm.redact import Redactor  # noqa: PLC0415
        from supportagent.domain.slots import SlotName  # noqa: PLC0415

        return LLMClient(
            provider=provider,
            redactor=Redactor(detectors=built_in_detectors()),
            prompts=PromptRegistry(root=_REPO_ROOT / "prompts"),
            events=events,
            ids=ids,
            clock=clock,
            slots={
                SlotName.CLASSIFY: SlotSpec(
                    model_id="claude-sonnet-4-5-20250929",
                    timeout_s=15.0,
                    max_output_tokens=400,
                )
            },
        )

    return _build


@pytest.fixture
def structured_llm(fake_llm: Any, _build_llm_client: Any) -> Any:
    """A fully-wired ``LLMClient`` over ``fake_llm`` (WP-007, 04 §3)."""
    return _build_llm_client(fake_llm)
