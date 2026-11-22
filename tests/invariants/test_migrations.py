"""Migration roundtrip + schema/domain enum parity (03 §4, 03 §1, 02 §4).

``test_migrations_roundtrip`` exercises the canonical CI sequence on both
engines: ``upgrade head -> downgrade base -> upgrade head`` (03 §4). It runs
against a scratch database (fresh sqlite file, or a fresh DB inside the
session's container) so it can tear the schema down and rebuild it without
corrupting the shared ``db`` session (04 §3).

``test_schema_enums_match_domain`` guards the 03 §1 rule that CHECK-constraint
literal sets are generated from the domain enums: if a domain enum member is
added or removed and the migration is not regenerated, this fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.engine import Engine

from supportagent.adapters.db.engine import make_engine
from supportagent.adapters.db.tables import metadata
from supportagent.bootstrap import run_migrations
from supportagent.domain.errors import DomainValidationError
from supportagent.domain.events import EventKind
from supportagent.domain.lifecycle import CaseStatus
from supportagent.domain.severity import Severity

# 10-wp / 03 §4: the exact table set at head (0001 .. 0007, plus the
# alembic bookkeeping table).
EXPECTED_TABLES = frozenset(
    {
        "users",
        "role_assignments",
        "case_groups",
        "rca_artifacts",
        "sync_journal_seen",
        "doctor_reports",
        "graph_facts",
        "graph_questions",
        "onboarding_requests",
        "onboarding_readiness",
        "onboarding_status",
        "onboarding_control",
        "component_summaries",
        "detector_review",
        "detector_watch",
        "cases",
        "alerts",
        "blobs",
        "tenant_keys",
        "leases",
        "events",
        # 10-wp human gate (0003).
        "proposals",
        "approvals",
        "policy_grants",
        "comms_plans",
        "outbox",
        "comms",
        # 03 §2.5 sync state (0004).
        "sync_cursors",
        "sync_seen",
        "automation_actors",
        # 03 §2.6 shadow counterfactual (0006).
        "shadow_records",
        # WP-102 owner drafts (0007).
        "comms_drafts",
        # WP-113 webhooks (0009).
        "webhook_nonces",
        "webhook_inbox",
        # WP-109 feedback (0010).
        "feedback",
        # WP-108 case memory (0011).
        "nodes",
        "edges",
        "kb_docs",
        "alembic_version",
    }
)


def _table_names(engine: Engine) -> frozenset[str]:
    return frozenset(sa_inspect(engine).get_table_names())


def _scratch_url(backend: str, db: Engine, tmp_path: Path) -> str:
    """A scratch URL on the same backend: a fresh file (sqlite) or a fresh DB
    in the session container (postgres)."""
    if backend == "sqlite":
        return f"sqlite:///{tmp_path}/scratch.db"
    # CREATE/DROP DATABASE cannot run inside a transaction block on Postgres,
    # so these admin statements run on an AUTOCOMMIT engine.
    admin = db.execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql("DROP DATABASE IF EXISTS sa_scratch")
        conn.exec_driver_sql("CREATE DATABASE sa_scratch")
    # NB: ``str(URL)`` (i.e. ``URL.__repr__``) masks the password as ``***``,
    # which would be handed to the engine as a *literal* password. Render
    # with ``hide_password=False`` to keep the real credentials.
    return db.url.set(database="sa_scratch").render_as_string(hide_password=False)


def _drop_scratch(backend: str, db: Engine) -> None:
    if backend != "postgres":
        return
    # Same AUTOCOMMIT constraint as _scratch_url (see there).
    admin = db.execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql("DROP DATABASE IF EXISTS sa_scratch")


def test_migrations_roundtrip(request: pytest.FixtureRequest, db: Engine, tmp_path: Path) -> None:
    """03 §4: ``upgrade head -> downgrade base -> upgrade head`` on both engines."""
    backend = request.config.getoption("--db")
    url = _scratch_url(backend, db, tmp_path)
    try:
        run_migrations(url)  # upgrade head
        engine = make_engine(url)
        assert _table_names(engine) == EXPECTED_TABLES, (
            "head must create the full 0001+0002+0003 schema"
        )
        engine.dispose()

        run_migrations(url, "base", allow_downgrade=True)  # downgrade base (CI roundtrip)
        engine = make_engine(url)
        assert _table_names(engine) == frozenset({"alembic_version"}), (
            "base must leave only alembic_version"
        )
        engine.dispose()

        run_migrations(url)  # upgrade head again
        engine = make_engine(url)
        assert _table_names(engine) == EXPECTED_TABLES, "re-upgrade must rebuild the schema"
        engine.dispose()
    finally:
        _drop_scratch(backend, db)


def test_migrate_refuses_downgrade_without_flag(
    request: pytest.FixtureRequest, db: Engine, tmp_path: Path
) -> None:
    """02 §4: ``migrate`` refuses a downgrade without ``--allow-downgrade``.

    At the bootstrap level the refusal surfaces as a DomainValidationError;
    the CLI maps it to exit code 2.
    """
    backend = request.config.getoption("--db")
    url = _scratch_url(backend, db, tmp_path)
    try:
        run_migrations(url)
        with pytest.raises(DomainValidationError, match="allow-downgrade"):
            run_migrations(url, "base")
        # State unchanged: still at head.
        engine = make_engine(url)
        assert _table_names(engine) == EXPECTED_TABLES
        engine.dispose()
    finally:
        _drop_scratch(backend, db)


def _check_literals(engine: Engine, table: str, column: str) -> frozenset[str]:
    """The quoted literal set of ``CHECK (<column> IN (...))`` as stored by
    either dialect (sqlite_master DDL, or pg_get_constraintdef)."""
    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            ddl = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :n"),
                {"n": table},
            ).scalar_one()
    else:
        with engine.connect() as conn:
            defs = (
                conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                        "JOIN pg_class r ON r.oid = c.conrelid "
                        "JOIN pg_namespace n ON n.oid = r.relnamespace "
                        "WHERE r.relname = :t AND n.nspname = current_schema() AND c.contype = 'c'"
                    ),
                    {"t": table},
                )
                .scalars()
                .all()
            )
        ddl = " ".join(defs)
    # Postgres stores ``IN`` as ``= ANY (ARRAY[...])``; SQLite keeps ``IN``.
    m = re.search(re.escape(column) + r"[^=]*=\s*ANY\s*\(\s*ARRAY\[(.*?)\]", ddl) or re.search(
        re.escape(column) + r"\s+IN\s*\(([^)]*)\)", ddl
    )
    if m is None:
        return frozenset()
    return frozenset(re.findall(r"'([^']*)'", m.group(1)))


def test_schema_enums_match_domain(db: Engine) -> None:
    """03 §1: CHECK literal sets must equal the domain enums — drift fails here."""
    assert _check_literals(db, "cases", "status") == frozenset(s.value for s in CaseStatus)
    assert _check_literals(db, "cases", "severity") == frozenset(s.value for s in Severity)
    # 03 §2.2: CHECK (origin IN ('alert','ticket')); cf. domain.model.alert.AlertEvent.origin.
    assert _check_literals(db, "cases", "origin") == frozenset({"alert", "ticket"})
    # 03 §2.1: role literals (a domain Role enum lands in v0.1, WP-103).
    assert _check_literals(db, "role_assignments", "role") == frozenset(
        {"viewer", "analyst", "approver", "admin"}
    )
    # 03 §2.4: events.kind generated from domain.EventKind; status from the
    # Event.status literal set (01 §4).
    assert _check_literals(db, "events", "kind") == frozenset(k.value for k in EventKind)
    assert _check_literals(db, "events", "status") == frozenset({"ok", "error", "skipped"})

    # The metadata object is the single DDL source and names exactly the
    # 0001+0002+0003 tables.
    assert frozenset(t.name for t in metadata.tables.values()) == EXPECTED_TABLES - {
        "alembic_version"
    }
