"""Read models for the admin API and UI (02 §2, §3; 10-wp WP-013).

Plain, tenant-scoped reads: proposals (the approvals queue sorts by severity
then age), one proposal with its exact payload, a case with its decision,
proposals, outbox rows and an event-kind summary, grants and users. Nothing
here writes. Event payloads are encrypted at rest, so the case view counts
events by kind; the full timeline is the trace replay.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol, Self

import sqlalchemy as sa

from supportagent.domain.errors import NotFound
from supportagent.domain.events import EventKind
from supportagent.domain.model.event import Event

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
    service: str | None = None

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
            "service": self.service,
        }


@dataclass(frozen=True, slots=True)
class CaseView:
    case: dict[str, object]
    decision: dict[str, object] | None
    proposals: list[ProposalView]
    outbox: list[dict[str, object]]
    event_counts: dict[str, int]
    comms_plans: list[dict[str, object]] = field(default_factory=list)
    drafts: list[dict[str, object]] = field(default_factory=list)
    #: WP-110: the RCA-lite artifact (internal; contradicted claims shown, never sent).
    rca: dict[str, object] | None = None
    #: BL-004: every customer update for the case, with its outbox outcome.
    comms: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "case": self.case,
            "decision": self.decision,
            "proposals": [p.as_dict() for p in self.proposals],
            "outbox": self.outbox,
            "event_counts": self.event_counts,
            "comms_plans": self.comms_plans,
            "drafts": self.drafts,
            "rca": self.rca,
            "comms": self.comms,
        }


_PROPOSAL_SELECT = (
    "SELECT p.id, p.case_id, p.action_kind, p.scope, p.status, p.payload_json, "
    "p.payload_hash, p.trace_id, p.created_by, p.created_at_ts, p.expires_ts, "
    "c.severity, c.external_id, c.service FROM proposals p JOIN cases c ON c.id = p.case_id "
)


_CASE_SELECT = (
    "SELECT id, origin, source, ticket_number, external_id, service, classification,"
    " severity, status, decision_json, duplicate_count, opened_ts, last_alert_ts,"
    " resolved_ts FROM cases "
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
                        "SELECT id, action_kind, status, attempts, external_ref, last_error, "
                        "policy_grant_id, payload_hash "
                        "FROM outbox WHERE case_id = :id AND tenant_id = :tenant ORDER BY id"
                    ),
                    {"id": case_id, "tenant": self._tenant},
                ).mappings()
            ]
            plans = [
                dict(r)
                for r in conn.execute(
                    sa.text(
                        "SELECT id, proposal_id, template_id, cadence_minutes, status, plan_hash "
                        "FROM comms_plans WHERE case_id = :id AND tenant_id = :tenant ORDER BY id"
                    ),
                    {"id": case_id, "tenant": self._tenant},
                ).mappings()
            ]
            drafts = [
                {**dict(r), "body": json.loads(str(r["body_json"])).get("text", "")}
                for r in conn.execute(
                    sa.text(
                        "SELECT plan_id, trigger, interval_slot, body_json, created_ts "
                        "FROM comms_drafts WHERE case_id = :id AND tenant_id = :tenant "
                        "ORDER BY created_ts, interval_slot"
                    ),
                    {"id": case_id, "tenant": self._tenant},
                ).mappings()
            ]
            comms = [
                {**dict(r), "text": json.loads(str(r["body_json"])).get("text", "")}
                for r in conn.execute(
                    sa.text(
                        "SELECT c.id, c.kind, c.plan_id, c.status, c.sent_at_ts, c.body_json, "
                        "o.attempts, o.last_error FROM comms c JOIN outbox o ON o.id = c.outbox_id "
                        "WHERE c.case_id = :id AND c.tenant_id = :tenant ORDER BY c.id"
                    ),
                    {"id": case_id, "tenant": self._tenant},
                ).mappings()
            ]
            rca_raw = conn.execute(
                sa.text(
                    "SELECT artifact_json FROM rca_artifacts "
                    "WHERE case_id = :id AND tenant_id = :tenant"
                ),
                {"id": case_id, "tenant": self._tenant},
            ).scalar()
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
            comms_plans=plans,
            drafts=[{k: v for k, v in d.items() if k != "body_json"} for d in drafts],
            rca=json.loads(str(rca_raw)) if rca_raw else None,
            comms=[{k: v for k, v in c.items() if k != "body_json"} for c in comms],
        )

    def cases(
        self, *, service: str | None = None, status: str | None = None, limit: int = 200
    ) -> list[dict[str, object]]:
        """Recent cases, newest first (WP-225): what each alert or ticket became."""
        where = ["tenant_id = :tenant"]
        params: dict[str, object] = {"tenant": self._tenant, "limit": max(1, min(limit, 1000))}
        if service:
            where.append("service = :service")
            params["service"] = service
        if status:
            where.append("status = :status")
            params["status"] = status
        sql = (
            _CASE_SELECT
            + "WHERE "
            + " AND ".join(where)  # fixed clauses; every value is a bound parameter
            + " ORDER BY last_alert_ts DESC, id DESC LIMIT :limit"
        )
        with self._uow:
            rows = self._uow.connection.execute(sa.text(sql), params).mappings().all()
        out = []
        for r in rows:
            decision = json.loads(str(r["decision_json"])) if r["decision_json"] else {}
            out.append(
                {
                    **{k: r[k] for k in r if k != "decision_json"},
                    "outcome": decision.get("outcome") if isinstance(decision, dict) else None,
                    "reason": decision.get("reason") if isinstance(decision, dict) else None,
                }
            )
        return out

    def case_counts(self) -> dict[str, dict[str, int]]:
        """Per service: open and total cases (WP-225, the system view)."""
        sql = (
            "SELECT service, status, COUNT(*) AS n FROM cases WHERE tenant_id = :tenant"
            " AND service IS NOT NULL GROUP BY service, status"
        )
        with self._uow:
            rows = self._uow.connection.execute(sa.text(sql), {"tenant": self._tenant}).all()
        counts: dict[str, dict[str, int]] = {}
        for service, status, n in rows:
            entry = counts.setdefault(str(service), {"open": 0, "total": 0})
            entry["total"] += int(n)
            if str(status) not in ("resolved", "closed"):
                entry["open"] += int(n)
        return counts

    def doctor_report(self) -> dict[str, object] | None:
        """BL-031: the last saved ``doctor`` report for ``/ui/setup``, or ``None``."""
        with self._uow:
            row = (
                self._uow.connection.execute(
                    sa.text("SELECT report_json, ran_ts FROM doctor_reports WHERE tenant_id = :t"),
                    {"t": self._tenant},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return {**json.loads(str(row["report_json"])), "ran_ts": int(row["ran_ts"])}

    def grants(self, *, status: str | None = None) -> list[dict[str, object]]:
        sql = (
            "SELECT id, action_kind, scope, status, requested_by, requested_at_ts, granted_by, "
            "granted_at_ts, valid_to_ts, conditions_json FROM policy_grants "
            "WHERE tenant_id = :tenant"
        )
        params: dict[str, object] = {"tenant": self._tenant}
        if status is not None:
            sql += " AND status = :status"
            params["status"] = status
        with self._uow:
            rows = self._uow.connection.execute(
                sa.text(sql + " ORDER BY scope, action_kind, id"), params
            ).mappings()
            return [_grant(dict(r)) for r in rows]

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
            scopes: dict[str, dict[str, str]] = {}
            for user_id, role, scope in conn.execute(
                sa.text(
                    "SELECT user_id, role, scope FROM role_assignments WHERE tenant_id = :tenant "
                    "ORDER BY user_id, role"
                ),
                {"tenant": self._tenant},
            ):
                roles.setdefault(str(user_id), []).append(str(role))
                scopes.setdefault(str(user_id), {})[str(role)] = str(scope)
        return [
            {
                **dict(u),
                "roles": roles.get(str(u["id"]), []),
                "role_scopes": scopes.get(str(u["id"]), {}),
            }
            for u in users
        ]


def _grant(row: dict[str, Any]) -> dict[str, object]:
    """A grant row, with its ADR-0014 conditions parsed for display (``None`` if none)."""
    raw = row.pop("conditions_json", None)
    try:
        row["conditions"] = json.loads(raw) if raw else None
    except ValueError:
        row["conditions"] = {"unreadable": True}  # the gate treats it as covering nothing
    return row


def auto_timeline(view: CaseView, events: Sequence[Event]) -> list[dict[str, object]]:
    """ADR-0014 part 2: each SOP auto decision on the case, in order (``/ui`` timeline).

    One entry per gate event that carries an ``auto`` record: the grant, its
    conditions, the evidence it read and why it did or didn't cover the
    step; then the proposal, the send (status, external ref) and any
    retraction. ``events`` is the case's decrypted trace.
    """
    proposals = {p.id: p for p in view.proposals}
    sends = {str(o.get("payload_hash")): o for o in view.outbox}
    retractions = {
        str(p.payload.get("retracts")): p for p in view.proposals if p.payload.get("retracts")
    }
    out: list[dict[str, object]] = []
    for event in events:
        payload = event.payload
        auto = payload.get("auto")
        if event.kind is not EventKind.GATE or not isinstance(auto, Mapping):
            continue
        pid = str(payload.get("proposal_id"))
        proposal = proposals.get(pid)
        send = sends.get(proposal.payload_hash) if proposal is not None else None
        retraction = retractions.get(pid)
        live = retraction is not None and retraction.status not in ("rejected", "expired")
        out.append(
            {
                "ts": event.ts,
                "proposal_id": pid,
                "auto": payload.get("transition") == "grant_enqueued",
                "grant_id": auto.get("grant_id"),
                "conditions": auto.get("conditions"),
                "evidence": auto.get("evidence"),
                "refused": auto.get("refused") or [],
                "used_today": auto.get("used_today"),
                "kind": proposal.action_kind if proposal is not None else None,
                "status": proposal.status if proposal is not None else None,
                "sop": (
                    f"{proposal.payload.get('sop_doc')} / {proposal.payload.get('sop_step')}"
                    if proposal is not None
                    else None
                ),
                "revert": proposal.payload.get("revert") if proposal is not None else None,
                "send_status": send.get("status") if send is not None else None,
                "external_ref": send.get("external_ref") if send is not None else None,
                "retraction_id": retraction.id if retraction is not None else None,
                "retraction_status": retraction.status if retraction is not None else None,
                "can_retract": (
                    proposal is not None
                    and proposal.status == "executed"
                    and proposal.payload.get("revert") == "retraction_note"
                    and not live
                ),
            }
        )
    return out


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
        service=row["service"],
    )


__all__ = [
    "AdminQueries",
    "CaseView",
    "ProposalView",
    "auto_timeline",
]
