"""WP-M04: the postgres.* tools against a real Postgres 16 (``--db=postgres``).

Every fixed statement runs and its result matches the declared schema. The
session is read-only and time-limited: the reader refuses a write even when
it hides inside a ``WITH``, and a slow statement hits ``statement_timeout``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from supportagent.adapters.tools.postgres import POSTGRES_TOOLS, PsycopgReader
from supportagent.domain.errors import ToolError
from supportagent.domain.tools import schema_errors

pytestmark = pytest.mark.integration


@pytest.fixture
def reader(db: Any) -> PsycopgReader:
    if db.dialect.name != "postgresql":
        pytest.skip("the postgres tools need --db=postgres")
    dsn = db.url.render_as_string(hide_password=False).replace("postgresql+psycopg", "postgresql")
    return PsycopgReader({"app": dsn}, statement_timeout_ms=500)


def test_every_postgres_tool_runs_and_matches_its_schema(reader: PsycopgReader) -> None:
    for tool_cls in POSTGRES_TOOLS:
        tool = tool_cls(reader=reader)
        data = tool.run({"database": "app"}, timeout_s=5).data
        assert schema_errors(dict(data), tool.result_schema) == [], tool.name
    activity = POSTGRES_TOOLS[0](reader=reader).run({"database": "app"}, timeout_s=5).data
    assert int(str(activity["max_connections"])) > 0


def test_postgres_session_is_read_only(reader: PsycopgReader, db: Any) -> None:
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS wp_m04_probe (id int)"))
    with pytest.raises(ToolError, match=r"read-only|ReadOnly"):
        reader.query(
            "app",
            "WITH d AS (DELETE FROM wp_m04_probe RETURNING 1) SELECT count(*) FROM d",
            timeout_s=5,
        )
    with db.begin() as conn:
        conn.execute(text("DROP TABLE wp_m04_probe"))


def test_postgres_tool_statement_timeout(reader: PsycopgReader) -> None:
    with pytest.raises(ToolError, match=r"QueryCanceled|cancel"):
        reader.query("app", "SELECT pg_sleep(2)", timeout_s=5)
