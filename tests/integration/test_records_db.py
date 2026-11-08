"""ADR-0021 §4: ``records.query`` against a real Postgres (``--db=postgres``).

The reader's session is read-only even for a write hidden in a ``WITH``, and
a traced id is a bound parameter, so a hostile one finds nothing rather than
changing the query. End to end, a reviewed flow whose last stage is
established from an order table traces an order: only the stage's fields
reach the evidence, PII is pseudonymised, and a rejected status is the
stage's rejection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr
from sqlalchemy import Engine, text

from supportagent.adapters.tools.records import PsycopgRecordReader, RecordsQueryTool
from supportagent.bootstrap import trace_object
from supportagent.config.schema import ToolPostgresDatabase, ToolRecords
from supportagent.domain.errors import ToolError
from tests.unit.test_pii import RegexAnalyzer
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.integration

ORDER_SQL = (
    "SELECT status, placed_at, note, secret FROM wp_records_orders "
    "WHERE id = :id ORDER BY placed_at"
)


@pytest.fixture
def dsn(db: Any) -> Any:
    if db.dialect.name != "postgresql":
        pytest.skip("records.query needs --db=postgres")
    with db.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS wp_records_orders"))
        conn.execute(
            text(
                "CREATE TABLE wp_records_orders (id text, symbol text, status text, "
                "placed_at timestamptz, note text, secret text)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO wp_records_orders VALUES "
                "('O-1', 'BTCUSDT', 'FILLED', '2026-11-08T01:00:00Z', 'placed for Jane Doe', 's1'),"
                "('O-2', 'ETHUSDT', 'REJECTED', '2026-11-08T01:05:00Z', 'max notional', 's2')"
            )
        )
    yield db.url.render_as_string(hide_password=False).replace("postgresql+psycopg", "postgresql")
    with db.begin() as conn:
        conn.execute(text("DROP TABLE wp_records_orders"))


def _tool(dsn: str) -> RecordsQueryTool:
    return RecordsQueryTool(reader=PsycopgRecordReader({"app": dsn}, statement_timeout_ms=2000))


def test_reads_with_the_id_bound(dsn: str) -> None:
    tool = _tool(dsn)
    found = tool.run({"database": "app", "sql": ORDER_SQL, "id": "O-1"}, timeout_s=5).data
    assert found["count"] == 1
    row = found["rows"][0]  # type: ignore[index]
    assert row["status"] == "FILLED"
    assert row["placed_at"].startswith("2026-11-08T01:00:00")
    hostile = tool.run({"database": "app", "sql": ORDER_SQL, "id": "x' OR '1'='1"}, timeout_s=5)
    assert hostile.data["count"] == 0
    like = "SELECT count(*)::int AS n FROM wp_records_orders WHERE symbol LIKE 'BTC%'"
    assert tool.run({"database": "app", "sql": like}, timeout_s=5).data["rows"] == [{"n": 1}]


def test_the_session_is_read_only(dsn: str) -> None:
    sql = "WITH d AS (DELETE FROM wp_records_orders RETURNING 1) SELECT count(*) FROM d"
    with pytest.raises(ToolError, match=r"ReadOnly|read-only"):
        _tool(dsn).run({"database": "app", "sql": sql}, timeout_s=5)


def test_trace_an_order_through_its_records(
    tmp_path: Path, dsn: str, db: Engine, master_key: bytes
) -> None:
    logs = tmp_path / "logs" / "submit"
    logs.mkdir(parents=True)
    (logs / "app.log").write_text("order O-1 submitted\norder O-2 submitted\n")
    catalog = tmp_path / "services.yaml"
    catalog.write_text(
        yaml.safe_dump(
            {
                "version": "1",
                "services": [
                    {"name": "submit", "log_paths": ["submit"]},
                    {"name": "oms", "databases": ["app"]},
                ],
            }
        )
    )
    flows = tmp_path / "flows"
    flows.mkdir()
    flow = {
        "flow": "order",
        "title": "Order from submission to booking",
        "object": "order",
        "id_pattern": r"O-\d+",
        "reviewed": True,
        "stages": [
            {
                "id": "submitted",
                "title": "Submitted",
                "component": "service:submit",
                "lookup": {
                    "capability": "logs.grep",
                    "source": "logs",
                    "args": {"path": "$service.log_paths[0]", "pattern": "order $object.id "},
                    "found": "matches[]",
                    "fields": ["matches[].text"],
                },
            },
            {
                "id": "booked",
                "title": "Booked",
                "component": "service:oms",
                "terminal": True,
                "rejection": {"path": "rows[].status", "values": ["REJECTED"]},
                "lookup": {
                    "capability": "records.query",
                    "source": "records",
                    "records_reason": "the OMS logs no order ids; the status is only in its table",
                    "args": {
                        "database": "$service.databases[0]",
                        "sql": ORDER_SQL,
                        "id": "$object.id",
                    },
                    "found": "rows[]",
                    "at": "rows[].placed_at",
                    "fields": ["rows[].status", "rows[].placed_at", "rows[].note"],
                },
            },
        ],
    }
    (flows / "order.yaml").write_text(yaml.safe_dump(flow))
    base = _shadow_config()
    records = ToolRecords(databases={"app": ToolPostgresDatabase(dsn=SecretStr(dsn))})
    config = base.model_copy(
        update={
            "paths": base.paths.model_copy(update={"catalog": str(catalog)}),
            "tools": base.tools.model_copy(
                update={"logs_root": str(tmp_path / "logs"), "records": records}
            ),
            "tracing": base.tracing.model_copy(
                update={"flows_dir": str(flows), "record_flows": ["order"]}
            ),
        }
    )
    run, _ = trace_object(
        config,
        "order",
        "O-1",
        engine=db,
        master_key=master_key,
        pii=RegexAnalyzer(),
        explain=False,
    )
    assert run.verdict.kind == "complete"
    booked = run.evidence["booked"]["rows"][0]  # type: ignore[index]
    assert booked["note"] == "placed for <PERSON_1>"
    assert "secret" not in booked  # not on the stage's field allow-list

    rejected, _ = trace_object(
        config, "order", "O-2", engine=db, master_key=master_key, explain=False
    )
    assert (rejected.verdict.kind, rejected.verdict.stage) == ("rejected", "booked")
