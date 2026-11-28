"""What the agent's own RCA concluded on live incidents, in shadow (WP-O6c).

Some causes can't be replayed: their evidence is point-in-time (Oracle locks
and sessions without the Diagnostics Pack, ADR-0018) and was gone before
onboarding looked. They are proven live instead. In shadow mode the RCA job
runs on a case of a class that isn't ready yet (``class-not-ready``), with the
bound, read-only credentials (ADR-0019), while the incident is happening. Once
the ticket is resolved and onboarding maps it to its class, readiness counts
that RCA's result as the incident's outcome.

Every resolved case also says what triage concluded on it, live. That is
calibration evidence (ADR-0002): once onboarding maps the ticket to its class,
the label it predicted was right or wrong, and the fitted calibration grows
by one observation. A label routes once enough of them agree.

Only resolved cases count: the class an incident belongs to is known from its
resolution, never from what the agent guessed while it was open.

A case with no ticket (an alerts-only system, ADR-0024) is resolved by a person
who records its outcome on the case page: what it really was, and whether the
RCA's cause was the real one. :meth:`LiveProof.alert_outcomes` reads those.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa

from supportagent.domain.outcomes import AlertOutcome


class _UoW(Protocol):
    @property
    def connection(self) -> sa.Connection: ...

    def commit(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class LiveRca:
    """One resolved case, live: triage's labels, and its RCA's verdict if it had one."""

    ticket: str
    #: ``None``: no RCA ran on it.
    verdict: str | None
    supported: str | None
    tested: tuple[str, ...]
    #: What triage said, and the pre-classifier's label.
    classification: str | None
    preclass: str | None
    severity: str | None


_ALERT_RESOLVED = (
    "SELECT c.id, c.classification, c.severity, c.decision_json, r.outcome, r.artifact_json "
    "FROM cases c LEFT JOIN rca_artifacts r ON r.tenant_id = c.tenant_id AND r.case_id = c.id "
    "WHERE c.tenant_id = :t AND c.status = 'resolved' AND c.ticket_number IS NULL "
    "ORDER BY c.opened_ts, c.id"
)
_CASE_OUTCOMES = (
    "SELECT case_id, payload FROM feedback WHERE tenant_id = :t AND kind = 'outcome' "
    "ORDER BY ts, id"
)


def _supported(artifact: dict[str, object], verdict: str | None) -> str | None:
    evidence = artifact.get("evidence")
    by_step = {
        str(e.get("step")): str(e.get("hypothesis"))
        for e in (evidence if isinstance(evidence, list) else [])
        if isinstance(e, dict)
    }
    found = by_step.get(str(artifact.get("supported_by") or ""))
    return found if verdict == "SUPPORTED" else None


_RESOLVED = (
    "SELECT c.ticket_number, c.classification, c.severity, c.decision_json, "
    "r.outcome, r.artifact_json FROM cases c "
    "LEFT JOIN rca_artifacts r ON r.tenant_id = c.tenant_id AND r.case_id = c.id "
    "WHERE c.tenant_id = :t AND c.status = 'resolved' AND c.ticket_number IS NOT NULL "
    "ORDER BY c.opened_ts, c.id"
)


class LiveProof:
    def __init__(self, *, uow: _UoW, tenant_id: str) -> None:
        self._uow = uow
        self._tenant = tenant_id

    def outcomes(self) -> dict[str, LiveRca]:
        """By ticket number; the first case wins when a ticket was triaged twice."""
        with self._uow:
            rows = self._uow.connection.execute(sa.text(_RESOLVED), {"t": self._tenant}).mappings()
            out: dict[str, LiveRca] = {}
            for row in rows:
                ticket = str(row["ticket_number"])
                if ticket in out:
                    continue
                artifact = json.loads(row["artifact_json"] or "{}")
                decision = json.loads(row["decision_json"] or "{}")
                by_step = {
                    str(e.get("step")): str(e.get("hypothesis"))
                    for e in artifact.get("evidence", [])
                    if isinstance(e, dict)
                }
                verdict = None if row["outcome"] is None else str(row["outcome"])
                supported = by_step.get(str(artifact.get("supported_by") or ""))
                suggestion = decision.get("llm_suggestion") or {}
                out[ticket] = LiveRca(
                    ticket=ticket,
                    verdict=verdict,
                    supported=supported if verdict == "SUPPORTED" else None,
                    tested=tuple(str(h) for h in artifact.get("hypotheses", [])),
                    classification=row["classification"]
                    or (suggestion.get("classification") if isinstance(suggestion, dict) else None),
                    preclass=decision.get("preclass_label"),
                    severity=row["severity"],
                )
        return out

    def alert_outcomes(self) -> list[AlertOutcome]:
        """ADR-0024: each resolved ticketless case with the outcome a person recorded."""
        with self._uow:
            conn = self._uow.connection
            recorded: dict[str, dict[str, object]] = {}
            for case_id, payload in conn.execute(sa.text(_CASE_OUTCOMES), {"t": self._tenant}):
                doc = json.loads(str(payload))
                if doc.get("source") == "case":  # the last one wins
                    recorded[str(case_id)] = doc
            rows = conn.execute(sa.text(_ALERT_RESOLVED), {"t": self._tenant}).mappings()
            out: list[AlertOutcome] = []
            seen: set[str] = set()
            for row in rows:
                case_id = str(row["id"])
                doc = recorded.get(case_id)
                if doc is None or case_id in seen or not isinstance(doc.get("classification"), str):
                    continue
                seen.add(case_id)
                artifact = json.loads(row["artifact_json"] or "{}")
                decision = json.loads(row["decision_json"] or "{}")
                suggestion = decision.get("llm_suggestion") or {}
                verdict = None if row["outcome"] is None else str(row["outcome"])
                cause = doc.get("cause")
                severity = doc.get("severity")
                out.append(
                    AlertOutcome(
                        case_id=case_id,
                        label=str(doc["classification"]),
                        severity=str(severity) if severity else None,
                        cause=str(cause) if cause in ("right", "wrong") else None,
                        drill=bool(doc.get("drill")),
                        predicted=row["classification"]
                        or decision.get("classification")
                        or (
                            suggestion.get("classification")
                            if isinstance(suggestion, dict)
                            else None
                        ),
                        preclass=decision.get("preclass_label"),
                        outcome=str(decision.get("outcome") or "safe_default"),
                        reason=str(decision.get("reason") or ""),
                        triage_severity=row["severity"],
                        verdict=verdict,
                        supported=_supported(artifact, verdict),
                    )
                )
        return out


__all__ = ["AlertOutcome", "LiveProof", "LiveRca"]
