"""What the agent's own RCA concluded on live incidents, in shadow (WP-O6c).

Some causes can't be replayed: their evidence is point-in-time (Oracle locks
and sessions without the Diagnostics Pack, ADR-0018) and was gone before
onboarding looked. They are proven live instead. In shadow mode the RCA job
runs on a case of a class that isn't ready yet (``class-not-ready``), with the
bound, read-only credentials (ADR-0019), while the incident is happening. Once
the ticket is resolved and onboarding maps it to its class, readiness counts
that RCA's result as the incident's outcome.

Only resolved cases count: the class an incident belongs to is known from its
resolution, never from what the agent guessed while it was open.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa


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
    """One resolved case's live RCA: its verdict and the hypothesis its tools supported."""

    ticket: str
    verdict: str
    supported: str | None
    tested: tuple[str, ...]
    #: What triage said, and the pre-classifier's label.
    classification: str | None
    preclass: str | None
    severity: str | None


_RESOLVED_WITH_RCA = (
    "SELECT c.ticket_number, c.classification, c.severity, c.decision_json, "
    "r.outcome, r.artifact_json FROM cases c "
    "JOIN rca_artifacts r ON r.tenant_id = c.tenant_id AND r.case_id = c.id "
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
            rows = self._uow.connection.execute(
                sa.text(_RESOLVED_WITH_RCA), {"t": self._tenant}
            ).mappings()
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
                verdict = str(row["outcome"])
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


__all__ = ["LiveProof", "LiveRca"]
