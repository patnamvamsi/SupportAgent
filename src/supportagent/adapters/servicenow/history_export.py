"""A system's ServiceNow history for onboarding (ADR-0015 P1, WP-O2), read-only.

For the assignment groups in the manifest's scope:

- their incidents opened within ``history_months`` (dot-walked references:
  ``cmdb_ci.name``, ``assignment_group.name``, ``problem_id.number``,
  ``caused_by.number``);
- the problems those incidents reference;
- the changes on the incidents' CIs in the same window, plus the changes
  that caused an incident;
- the CMDB records of those CIs and one hop of ``cmdb_rel_ci`` around them.

The queries are plain encoded queries (``IN``, ``>=``), no server-side
scripts, so they run the same on any instance. Text is returned as-is;
onboarding redacts it before storing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from supportagent.adapters.servicenow.http import ServiceNowHTTP
from supportagent.adapters.servicenow.table_export import export_table
from supportagent.domain.onboarding import TicketHistory

INCIDENT_FIELDS = (
    "number",
    "sys_id",
    "opened_at",
    "resolved_at",
    "priority",
    "cmdb_ci.name",
    "category",
    "assignment_group.name",
    "short_description",
    "description",
    "state",
    "close_code",
    "close_notes",
    "problem_id.number",
    "caused_by.number",
)
_RENAME = {
    "cmdb_ci.name": "cmdb_ci",
    "assignment_group.name": "assignment_group",
    "problem_id.number": "problem_id",
    "caused_by.number": "caused_by",
    "parent.name": "parent",
    "type.name": "type",
    "child.name": "child",
}
_CHANGE_FIELDS = ("number", "short_description", "cmdb_ci.name", "start_date", "type", "state")
_CI_FIELDS = ("sys_id", "name", "sys_class_name", "install_status")
_REL_FIELDS = ("sys_id", "parent.name", "type.name", "child.name")
_MONTH = timedelta(days=30.44)
_SIDE_LIMIT = 2000
#: ``install_status`` choices, so a CMDB record read without display values reads as a word.
INSTALL_STATUS = {
    "1": "Installed",
    "2": "On Order",
    "3": "In Maintenance",
    "4": "Pending Install",
    "5": "Pending Repair",
    "6": "In Stock",
    "7": "Retired",
    "8": "Stolen",
    "100": "Absent",
}


def export_history(
    http: ServiceNowHTTP,
    *,
    assignment_groups: Sequence[str],
    history_months: int,
    max_incidents: int,
    now: datetime,
) -> TicketHistory:
    since = (now - history_months * _MONTH).strftime("%Y-%m-%d %H:%M:%S")
    rows, more = export_table(
        http,
        "incident",
        query=f"assignment_group.nameIN{','.join(assignment_groups)}"
        f"^opened_at>={since}^ORDERBYopened_at",
        fields=INCIDENT_FIELDS,
        limit=max_incidents,
    )
    incidents = [_renamed(r) for r in rows]
    problems = _problems(http, incidents)
    cis = sorted({i["cmdb_ci"] for i in incidents if i.get("cmdb_ci")})
    changes = _changes(http, incidents, cis, since)
    names = set(cis) | {c["cmdb_ci"] for c in changes if c.get("cmdb_ci")}
    ci_rows, relationships = _cmdb(http, sorted(names))
    return TicketHistory(incidents, problems, changes, ci_rows, relationships, truncated=more)


def _renamed(row: dict[str, str]) -> dict[str, str]:
    return {_RENAME.get(k, k): v for k, v in row.items() if v != ""}


def _problems(http: ServiceNowHTTP, incidents: Sequence[dict[str, str]]) -> list[dict[str, object]]:
    numbers = sorted({i["problem_id"] for i in incidents if i.get("problem_id")})
    if not numbers:
        return []
    rows, _ = export_table(
        http,
        "problem",
        query=f"numberIN{','.join(numbers)}",
        fields=("number", "short_description", "state"),
        limit=len(numbers),
    )
    related: dict[str, list[str]] = {}
    for incident in incidents:
        if incident.get("problem_id"):
            related.setdefault(incident["problem_id"], []).append(incident["number"])
    return [{**r, "related_incidents": related.get(r["number"], [])} for r in rows]


def _changes(
    http: ServiceNowHTTP, incidents: Sequence[dict[str, str]], cis: Sequence[str], since: str
) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    causing = sorted({i["caused_by"] for i in incidents if i.get("caused_by")})
    queries = []
    if cis:
        queries.append(f"cmdb_ci.nameIN{','.join(cis)}^start_date>={since}")
    if causing:
        queries.append(f"numberIN{','.join(causing)}")
    for query in queries:
        rows, _ = export_table(
            http, "change_request", query=query, fields=_CHANGE_FIELDS, limit=_SIDE_LIMIT
        )
        for row in rows:
            record = _renamed(row)
            found[record["number"]] = record
    return [found[n] for n in sorted(found)]


def _cmdb(
    http: ServiceNowHTTP, names: Sequence[str]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if not names:
        return [], []
    listed = ",".join(names)
    rels: dict[str, dict[str, str]] = {}
    for end in ("parent", "child"):
        rows, _ = export_table(
            http,
            "cmdb_rel_ci",
            query=f"{end}.nameIN{listed}",
            fields=_REL_FIELDS,
            limit=_SIDE_LIMIT,
        )
        for row in rows:
            rels[row["sys_id"]] = _renamed(row)
    wanted = sorted(
        set(names) | {r[e] for r in rels.values() for e in ("parent", "child") if e in r}
    )
    rows, _ = export_table(
        http, "cmdb_ci", query=f"nameIN{','.join(wanted)}", fields=_CI_FIELDS, limit=len(wanted)
    )
    cis = [
        {
            **r,
            "install_status": INSTALL_STATUS.get(
                r.get("install_status", ""), r.get("install_status", "")
            ),
        }
        for r in rows
    ]
    return sorted(cis, key=lambda c: c["name"]), [rels[k] for k in sorted(rels)]


__all__ = ["INCIDENT_FIELDS", "INSTALL_STATUS", "export_history"]
