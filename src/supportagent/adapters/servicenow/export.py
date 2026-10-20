"""Read a partner's ServiceNow history export for ``shadow import`` (10-wp WP-014).

JSONL, one incident per line::

    {"table": "incident",                       # optional, default "incident"
     "record": {<Table API fields: sys_id, number, short_description,
                 description, priority, cmdb_ci, opened_at, sys_updated_on,
                 sys_updated_by, sys_mod_count, ...>},
     "human": {"classification": "database/saturation", "severity": "P2",
               "first_action_at": "2026-01-02 10:04:00"}}

The record is mapped with the same ``record_to_alert`` as live sync (02 §6),
received at its ``opened_at``. ``human`` is the final classification and
severity a human assigned, which shadow compares against (agreement). Blank
lines are skipped; any malformed line is a ``ConfigError`` naming its line.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from supportagent.adapters.servicenow.mapping import _to_aware_utc, record_to_alert, row_to_record
from supportagent.domain.errors import ConfigError, ServiceNowError
from supportagent.domain.model.shadow import HistoricalIncident
from supportagent.domain.severity import Severity


def read_export(path: Path, *, tenant_id: str) -> Iterator[HistoricalIncident]:
    """Yield one ``HistoricalIncident`` per non-blank line of ``path``."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError(f"cannot read export {path}: {exc}") from exc  # noqa: TRY003
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            yield _incident(json.loads(line), tenant_id)
        except (ValueError, KeyError, TypeError, ValidationError, ServiceNowError) as exc:
            raise ConfigError(f"export {path} line {number}: {exc!r}") from exc  # noqa: TRY003


def _incident(item: dict[str, Any], tenant_id: str) -> HistoricalIncident:
    table = str(item.get("table") or "incident")
    row = item["record"]
    opened_at = _to_aware_utc(row["opened_at"])
    evt = record_to_alert(tenant_id, row_to_record(table, row), received_at=opened_at)
    human = item.get("human") or {}
    severity = human.get("severity")
    first_action = human.get("first_action_at")
    return HistoricalIncident(
        evt=evt,
        opened_at=opened_at,
        human_classification=human.get("classification"),
        human_severity=Severity(severity) if severity else None,
        human_first_action_at=_to_aware_utc(first_action) if first_action else None,
    )
