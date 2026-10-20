"""Read models for the admin API and UI (02 §2, §3; 10-wp WP-013).

Plain, tenant-scoped reads: proposals (the approvals queue sorts by severity
then age), one proposal with its exact payload, a case with its decision,
proposals, outbox rows and an event-kind summary, grants and users. Nothing
here writes. Event payloads are encrypted at rest, so the case view counts
events by kind; the full timeline is the trace replay.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, Self

import sqlalchemy as sa

from supportagent.domain.errors import NotFound

#: Approvals queue order (02 §3): P1 first; an unranked case sorts last.
_SEVERITY_ORDER = (
    "CASE c.severity WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 WHEN 'P4' THEN 4 ELSE 5 END"
)


class _UoW(Protocol):
    @property
    def connection(self) -> sa.Connection: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ProposalView:
    id: str
    case_id: str
    action_kind: str
    scope: str
    status: str
    payload: Mapping[str, Any]
    payload_json: str
    payload_hash: str
    trace_id: str
    created_by: str
    created_at_ts: int
    expires_ts: int | None
    severity: str | None
    target_ref: str | None

    @property
    def preview(self) -> str:
        """The human-readable text the action would post (work-note text in v0.0)."""
        text = self.payload.get("text")
        return text if isinstance(text, str) else self.payload_json

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "case_id": self.case_id,
            "action_kind": self.action_kind,
            "scope": self.scope,
            "status": self.status,
            "payload": dict(self.payload),
            "payload_hash": self.payload_hash,
            "preview": self.preview,
            "trace_id": self.trace_id,
            "trace_url": f"/api/v1/traces/{self.trace_id}",
            "created_by": self.created_by,
            "created_at_ts": self.created_at_ts,
            "expires_ts": self.expires_ts,
            "severity": self.severity,
            "target_ref": self.target_ref,
        }


@dataclass(frozen=True, slots=True)
class CaseView:
    case: dict[str, object]
    decision: dict[str, object] | None
    proposals: list[ProposalView]
    outbox: list[dict[str, object]]
    event_counts: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "case": self.case,
            "decision": self.decision,
            "proposals": [p.as_dict() for p in self.proposals],
            "outbox": self.outbox,
            "event_counts": self.event_counts,
        }


_PROPOSAL_SELECT = (
    "SELECT p.id, p.case_id, p.action_kind, p.scope, p.status, p.payload_json, "
    "p.payload_hash, p.trace_id, p.created_by, p.created_at_ts, p.expires_ts, "
    "c.severity, c.external_id FROM proposals p JOIN cases c ON c.id = p.case_id "
)


class AdminQueries:
    """Tenant-scoped reads for the admin surface."""

    def __init__(self, *, uow: _UoW, tenant_id: str) -> None:
        self._uow = uow
        self._tenant = tenant_id

    def proposals(
        self, *, status: str | None = None, case_id: str | None = None
    ) -> list[ProposalView]:
        where = ["p.tenant_id = :tenant"]
        params: dict[str, object] = {"tenant": self._tenant}
        if status is not None:
            where.append("p.status = :status")
            params["status"] = status
        if case_id is not None:
            where.append("p.case_id = :case_id")
            params["case_id"] = case_id
        sql = (
            _PROPOSAL_SELECT
            + "WHERE "
            + " AND ".join(where)
            + f" ORDER BY {_SEVERITY_ORDER}, p.created_at_ts, p.id"
        )
        with self._uow:
            rows = self._uow.connection.execute(sa.text(sql), params).mappings().all()
        return [_proposal(r) for r in rows]

    def proposal(self, proposal_id: str) -> ProposalView:
        with self._uow:
            row = (
                self._uow.connection.execute(
                    sa.text(_PROPOSAL_SELECT + "WHERE p.id = :id AND p.tenant_id = :tenant"),
                    {"id": proposal_id, "tenant": self._tenant},
                )
                .mappings()
                .first()
            )
        if row is None:
            raise NotFound(f"proposal not found: {proposal_id!r}")  # noqa: TRY003
        return _proposal(row)

    def case(self, case_id: str) -> CaseView:
        with self._uow:
            conn = self._uow.connection
            row = (
                conn.execute(
                    sa.text(
                        "SELECT id, fingerprint, origin, status, service, severity, "
                        "classification, external_table, external_id, duplicate_count, "
                        "opened_ts, last_alert_ts, trace_id, decision_json FROM cases "
                        "WHERE id = :id AND tenant_id = :tenant"
                    ),
                    {"id": case_id, "tenant": self._tenant},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound(f"case not found: {case_id!r}")  # noqa: TRY003
            outbox = [
                dict(r)
                for r in conn.execute(
                    sa.text(
                        "SELECT id, action_kind, status, attempts, external_ref, last_error "
                        "FROM outbox WHERE case_id = :id AND tenant_id = :tenant ORDER BY id"
                    ),
                    {"id": case_id, "tenant": self._tenant},
                ).mappings()
            ]
            counts = {
                str(kind): int(n)
                for kind, n in conn.execute(
                    sa.text(
                        "SELECT kind, COUNT(*) FROM events WHERE trace_id = :trace "
                        "AND tenant_id = :tenant GROUP BY kind ORDER BY kind"
                    ),
                    {"trace": row["trace_id"], "tenant": self._tenant},
                )
            }
        case = {k: v for k, v in dict(row).items() if k != "decision_json"}
        raw = row["decision_json"]
        decision = json.loads(raw) if raw else None
        return CaseView(
            case=case,
            decision=decision,
            proposals=self.proposals(case_id=case_id),
            outbox=outbox,
            event_counts=counts,
        )

    def grants(self, *, status: str | None = None) -> list[dict[str, object]]:
        sql = (
            "SELECT id, action_kind, scope, status, requested_by, requested_at_ts, granted_by, "
            "granted_at_ts, valid_to_ts FROM policy_grants WHERE tenant_id = :tenant"
        )
        params: dict[str, object] = {"tenant": self._tenant}
        if status is not None:
            sql += " AND status = :status"
            params["status"] = status
        with self._uow:
            rows = self._uow.connection.execute(
                sa.text(sql + " ORDER BY scope, action_kind, id"), params
            ).mappings()
            return [dict(r) for r in rows]

    def users(self) -> list[dict[str, object]]:
        with self._uow:
            conn = self._uow.connection
            users = (
                conn.execute(
                    sa.text(
                        "SELECT id, email, name, is_active, last_login_ts FROM users "
                        "WHERE tenant_id = :tenant ORDER BY id"
                    ),
                    {"tenant": self._tenant},
                )
                .mappings()
                .all()
            )
            roles: dict[str, list[str]] = {}
            for user_id, role in conn.execute(
                sa.text(
                    "SELECT user_id, role FROM role_assignments WHERE tenant_id = :tenant "
                    "ORDER BY user_id, role"
                ),
                {"tenant": self._tenant},
            ):
                roles.setdefault(str(user_id), []).append(str(role))
        return [{**dict(u), "roles": roles.get(str(u["id"]), [])} for u in users]


def _proposal(row: sa.RowMapping) -> ProposalView:
    return ProposalView(
        id=str(row["id"]),
        case_id=str(row["case_id"]),
        action_kind=str(row["action_kind"]),
        scope=str(row["scope"]),
        status=str(row["status"]),
        payload=json.loads(row["payload_json"]),
        payload_json=str(row["payload_json"]),
        payload_hash=str(row["payload_hash"]),
        trace_id=str(row["trace_id"]),
        created_by=str(row["created_by"]),
        created_at_ts=int(row["created_at_ts"]),
        expires_ts=int(row["expires_ts"]) if row["expires_ts"] is not None else None,
        severity=row["severity"],
        target_ref=row["external_id"],
    )


__all__ = ["AdminQueries", "CaseView", "ProposalView"]
