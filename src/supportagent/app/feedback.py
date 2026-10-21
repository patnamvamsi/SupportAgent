"""FeedbackService: the three feedback moments (ARCH §8.3, §8.4; 11-wp WP-109).

Each moment is a ``feedback`` row **and** a ``feedback`` event on the case's
trace, written in the caller's unit of work:

- **``approval``**: every approve or reject of a proposal, with the approver's
  reason (``ApprovalService``).
- **``diff``**: every **human** change to a tracked field, from the per-change
  history (ARCH §8.1, never the last writer). It's paired with what the agent
  proposed or decided, and gets a verdict:

  - ``priority`` vs the triage decision's severity → ``confirmed`` / ``contradicted``;
  - a field a ``ticket_update`` proposal would set → ``confirmed`` / ``contradicted``;
  - anything else tracked (``state``, ``assigned_to``, ``category``, ``subcategory``)
    → ``human_action``, which records what the human did with nothing to compare.

- **``outcome``**: at resolution, the final human-set severity (``priority``)
  and classification (``category/subcategory``, through
  ``servicenow.classification_map`` when one is configured, BL-035) from the diffs. The case's
  **shadow record** then gets its human labels, first-action time and agreement
  (CAP §4.7), filling only what it doesn't already have.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa

from supportagent.app.lifecycle import confirmed_automation_actors
from supportagent.domain.classification import map_servicenow_classification
from supportagent.domain.events import EventKind
from supportagent.domain.ids import IdPrefix
from supportagent.domain.model.event import Event
from supportagent.domain.model.ticket import ChangeRecord
from supportagent.domain.severity import Severity
from supportagent.domain.shadow import agreement
from supportagent.domain.takeover import is_human_actor
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator

TRACKED_FIELDS = frozenset({"priority", "state", "assigned_to", "category", "subcategory"})
_PRIORITY = {"1": Severity.P1, "2": Severity.P2, "3": Severity.P3, "4": Severity.P4}


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


class FeedbackService:
    def __init__(  # noqa: PLR0913 — the ports plus the actor identity
        self,
        *,
        uow: _UoW,
        clock: Clock,
        ids: IdGenerator,
        events: EventSink,
        tenant_id: str,
        integration_user: str = "",
        classification_map: Mapping[str, str] | None = None,
    ) -> None:
        self._uow = uow
        self._classification_map = dict(classification_map or {})
        self._clock = clock
        self._ids = ids
        self._events = events
        self._tenant = tenant_id
        self._integration_user = integration_user

    # --- approval -------------------------------------------------------------

    def record_approval(
        self, proposal_id: str, *, approver_id: str, decision: str, reason: str | None
    ) -> None:
        with self._uow:
            row = self._uow.connection.execute(
                sa.text(
                    "SELECT case_id, action_kind FROM proposals WHERE id = :id AND tenant_id = :t"
                ),
                {"id": proposal_id, "t": self._tenant},
            ).first()
            if row is None:
                return
            self._write(
                str(row[0]),
                "approval",
                approver_id,
                {
                    "proposal_id": proposal_id,
                    "action_kind": str(row[1]),
                    "decision": decision,
                    "reason": reason,
                },
            )
            self._uow.commit()  # deferred when joining the caller's UoW

    # --- diff -----------------------------------------------------------------

    def record_changes(
        self,
        case_id: str,
        changes: Sequence[ChangeRecord],
        *,
        automation_actors: Collection[str] | None = None,
    ) -> int:
        """One ``diff`` per human change of a tracked field; the number written."""
        written = 0
        with self._uow:
            case = self._case(case_id)
            if case is None:
                return 0
            if automation_actors is None:
                automation_actors = confirmed_automation_actors(self._uow.connection, self._tenant)
            decision = json.loads(str(case["decision_json"])) if case["decision_json"] else {}
            proposed = self._proposed_fields(case_id)
            for change in sorted(changes, key=lambda c: (c.ts, c.checkpoint or 0)):
                if change.source != "audit" or not change.fieldname:
                    continue
                if change.fieldname not in TRACKED_FIELDS and change.fieldname not in proposed:
                    continue
                if not is_human_actor(
                    change.user,
                    integration_user=self._integration_user,
                    automation_actors=automation_actors,
                ):
                    continue
                payload = self._diff(change, decision, proposed)
                self._write(case_id, "diff", change.user, payload, ts=change.ts)
                written += 1
            self._uow.commit()
        return written

    @staticmethod
    def _diff(
        change: ChangeRecord, decision: dict[str, object], proposed: dict[str, tuple[str, str]]
    ) -> dict[str, object]:
        field = str(change.fieldname)
        actual = change.newvalue
        payload: dict[str, object] = {
            "field": field,
            "actual": actual,
            "sys_mod_count": change.checkpoint,
            "at": change.ts.isoformat(),
        }
        if field in proposed:
            proposal_id, value = proposed[field]
            payload.update(
                paired_with=proposal_id,
                proposed=value,
                verdict="confirmed" if value == actual else "contradicted",
            )
        elif field == "priority" and decision.get("severity"):
            human = _PRIORITY.get(actual or "")
            payload.update(
                paired_with="decision",
                proposed=decision["severity"],
                verdict="confirmed"
                if human and human.value == decision["severity"]
                else "contradicted",
            )
        else:
            payload["verdict"] = "human_action"
        return payload

    # --- outcome --------------------------------------------------------------

    def record_outcome(self, case_id: str, *, resolved_by: str) -> None:
        """At resolution: the final human labels, plus the shadow record's human fields."""
        with self._uow:
            case = self._case(case_id)
            if case is None:
                return
            diffs = self._diffs(case_id)
            latest = {d["field"]: d for d in diffs}  # in time order: the last value wins
            priority = latest.get("priority", {}).get("actual")
            severity = _PRIORITY.get(str(priority)) if priority else None
            category = latest.get("category", {}).get("actual")
            subcategory = latest.get("subcategory", {}).get("actual")
            classification = map_servicenow_classification(
                str(category) if category else None,
                str(subcategory) if subcategory else None,
                self._classification_map,
            )
            first_at = min((datetime.fromisoformat(str(d["at"])) for d in diffs), default=None)
            self._write(
                case_id,
                "outcome",
                resolved_by,
                {
                    "resolution": "resolved",
                    "classification": classification,
                    "servicenow_category": (
                        "/".join(str(v) for v in (category, subcategory) if v) or None
                    ),
                    "severity": severity.value if severity else None,
                    "first_human_action_at": first_at.isoformat() if first_at else None,
                },
            )
            self._fill_shadow(case_id, classification, severity, first_at)
            self._uow.commit()

    def _fill_shadow(
        self,
        case_id: str,
        classification: str | None,
        severity: Severity | None,
        first_at: datetime | None,
    ) -> None:
        conn = self._uow.connection
        shadow = (
            conn.execute(
                sa.text(
                    "SELECT s.id, s.human_classification, s.human_severity, "
                    "s.human_first_action_ts, s.proposed_json FROM shadow_records s "
                    "WHERE s.case_id = :c AND s.tenant_id = :t"
                ),
                {"c": case_id, "t": self._tenant},
            )
            .mappings()
            .first()
        )
        if shadow is None:
            return
        human_class = shadow["human_classification"] or classification
        human_sev = shadow["human_severity"] or (severity.value if severity else None)
        first_ts = shadow["human_first_action_ts"] or (
            int(first_at.timestamp()) if first_at else None
        )
        decision = json.loads(str(shadow["proposed_json"])).get("decision", {})
        agreed = agreement(
            decision.get("classification"),
            Severity(decision["severity"]) if decision.get("severity") else None,
            human_class,
            Severity(human_sev) if human_sev else None,
        )
        conn.execute(
            sa.text(
                "UPDATE shadow_records SET human_classification = :hc, human_severity = :hs, "
                "human_first_action_ts = :ft, agreement = :ag WHERE id = :id"
            ),
            {
                "hc": human_class,
                "hs": human_sev,
                "ft": first_ts,
                "ag": None if agreed is None else int(agreed),
                "id": shadow["id"],
            },
        )

    # --- helpers --------------------------------------------------------------

    def _case(self, case_id: str) -> sa.RowMapping | None:
        return (
            self._uow.connection.execute(
                sa.text(
                    "SELECT id, trace_id, decision_json FROM cases "
                    "WHERE id = :id AND tenant_id = :t"
                ),
                {"id": case_id, "t": self._tenant},
            )
            .mappings()
            .first()
        )

    def _proposed_fields(self, case_id: str) -> dict[str, tuple[str, str]]:
        """Fields a ``ticket_update`` proposal of the case would set → (proposal, value)."""
        out: dict[str, tuple[str, str]] = {}
        rows = self._uow.connection.execute(
            sa.text(
                "SELECT id, payload_json FROM proposals WHERE case_id = :c AND tenant_id = :t "
                "AND action_kind = 'ticket_update' AND status IN "
                "('pending_approval', 'approved', 'executed') ORDER BY created_at_ts"
            ),
            {"c": case_id, "t": self._tenant},
        ).all()
        for proposal_id, payload_json in rows:
            fields = json.loads(payload_json).get("fields", {})
            if isinstance(fields, dict):
                for name, value in fields.items():
                    out[str(name)] = (str(proposal_id), str(value))
        return out

    def _diffs(self, case_id: str) -> list[dict[str, object]]:
        rows = self._uow.connection.execute(
            sa.text(
                "SELECT payload FROM feedback WHERE case_id = :c AND tenant_id = :t "
                "AND kind = 'diff' ORDER BY ts, id"
            ),
            {"c": case_id, "t": self._tenant},
        ).scalars()
        return [json.loads(str(r)) for r in rows]

    def _write(
        self,
        case_id: str,
        kind: str,
        actor: str,
        payload: dict[str, object],
        ts: datetime | None = None,
    ) -> None:
        when = ts or self._clock.now()
        feedback_id = self._ids.new(IdPrefix.FEEDBACK)
        self._uow.connection.execute(
            sa.text(
                "INSERT INTO feedback (id, tenant_id, case_id, kind, payload, ts) "
                "VALUES (:id, :t, :c, :k, :p, :ts)"
            ),
            {
                "id": feedback_id,
                "t": self._tenant,
                "c": case_id,
                "k": kind,
                "p": json.dumps(payload, sort_keys=True),
                "ts": int(when.astimezone(UTC).timestamp()),
            },
        )
        trace = self._uow.connection.execute(
            sa.text("SELECT trace_id FROM cases WHERE id = :c"), {"c": case_id}
        ).scalar_one()
        event_id = self._ids.new(IdPrefix.EVENT)
        self._events.append(
            Event(
                id=event_id,
                tenant_id=self._tenant,
                trace_id=str(trace),
                span_id=f"feedback_{event_id}",
                parent_span_id=None,
                kind=EventKind.FEEDBACK,
                actor=actor[:64],
                payload={
                    "v": 1,
                    "kind": kind,
                    "feedback_id": feedback_id,
                    **{k: v for k, v in payload.items() if k != "reason"},
                },
                ts=self._clock.now(),
            )
        )


__all__ = ["TRACKED_FIELDS", "FeedbackService"]
