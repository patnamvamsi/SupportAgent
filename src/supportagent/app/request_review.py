"""Reviewing onboarding's access requests (ADR-0015 §4, WP-O5).

The gap plan proposes requests; a person approves (they will provide the
credential the request describes) or declines each one.

- :meth:`RequestReview.save` upserts a plan's requests in rank order. A re-plan
  keeps each decision; a request the new plan no longer makes stays, marked
  not ``present``.
- :meth:`RequestReview.decide` records a decision.
- :meth:`RequestReview.record_verification` (ADR-0019, WP-O5b) records a
  binding's probes: ``bound`` when the credential was proven read-only, or the
  request reopens with why. ``app.access_binding`` runs them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, Protocol, Self

import sqlalchemy as sa

from supportagent.app.onboarding_audit import OnboardingAudit
from supportagent.domain.errors import DomainValidationError, NotFound
from supportagent.domain.gap_plan import AccessRequest, GapPlan
from supportagent.ports.clock import Clock

RequestDecision = Literal["approved", "declined", "open"]
#: A request's status: a decision, or ``bound`` once its credential was proven read-only.
RequestStatus = Literal["approved", "declined", "open", "bound"]


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


def request_id(plugin: str, access: str, target: str) -> str:
    return hashlib.sha256(f"{plugin}\x00{access}\x00{target}".encode()).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class RequestView:
    id: str
    plugin: str
    access: str
    target: str
    scope: str
    description: str
    found_in: str
    why: str
    unlocks: tuple[str, ...]
    share: float
    verify_can: tuple[str, ...]
    verify_cannot: tuple[str, ...]
    status: str
    present: bool
    decided_by: str | None
    #: ADR-0019: the last binding's verdict and probes (``at``, ``bound``, ``why``, ``probes``).
    verification: dict[str, object] | None = None

    @property
    def reopened_why(self) -> str | None:
        """Why the last credential was refused, while the request is open again."""
        v = self.verification
        if self.status != "open" or not v or v.get("bound"):
            return None
        return str(v.get("why") or "")


def _detail(r: AccessRequest) -> dict[str, object]:
    return {
        "scope": r.scope,
        "description": r.description,
        "found_in": r.found_in,
        "why": r.why,
        "unlocks": list(r.unlocks),
        "verify_can": list(r.verify_can),
        "verify_cannot": list(r.verify_cannot),
    }


class RequestReview:
    def __init__(
        self,
        *,
        uow: _UoW,
        clock: Clock,
        tenant_id: str,
        audit: OnboardingAudit | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._tenant = tenant_id
        #: ADR-0017 (WP-A6): each decision is an ``onboarding`` event, in the same transaction.
        self._audit = audit

    def save(self, plan: GapPlan) -> dict[str, int]:
        now = int(self._clock.now().timestamp())
        counts = {"new": 0, "kept": 0, "gone": 0}
        with self._uow:
            conn = self._uow.connection
            seen: list[str] = []
            for rank, r in enumerate(plan.requests):
                rid = request_id(*r.key)
                seen.append(rid)
                values = {
                    "id": rid,
                    "t": self._tenant,
                    "plugin": r.plugin,
                    "access": r.access,
                    "target": r.target,
                    "detail": json.dumps(_detail(r), sort_keys=True),
                    "share": r.share,
                    "rank": rank,
                    "now": now,
                }
                updated = conn.execute(
                    sa.text(
                        "UPDATE onboarding_requests SET detail_json = :detail, share = :share, "
                        "rank = :rank, present = 1, updated_ts = :now "
                        "WHERE id = :id AND tenant_id = :t"
                    ),
                    values,
                ).rowcount
                if updated:
                    counts["kept"] += 1
                    continue
                conn.execute(
                    sa.text(
                        "INSERT INTO onboarding_requests (id, tenant_id, plugin, access, target, "
                        "detail_json, share, rank, updated_ts) VALUES (:id, :t, :plugin, "
                        ":access, :target, :detail, :share, :rank, :now)"
                    ),
                    values,
                )
                counts["new"] += 1
            stale = [
                row[0]
                for row in conn.execute(
                    sa.text(
                        "SELECT id FROM onboarding_requests WHERE tenant_id = :t AND present = 1"
                    ),
                    {"t": self._tenant},
                )
                if row[0] not in seen
            ]
            for rid in stale:
                conn.execute(
                    sa.text(
                        "UPDATE onboarding_requests SET present = 0, updated_ts = :now "
                        "WHERE id = :id AND tenant_id = :t"
                    ),
                    {"id": rid, "t": self._tenant, "now": now},
                )
            counts["gone"] = len(stale)
            self._uow.commit()
        return counts

    def requests(self, *, include_gone: bool = False) -> list[RequestView]:
        with self._uow:
            rows = self._uow.connection.execute(
                sa.text(
                    "SELECT * FROM onboarding_requests WHERE tenant_id = :t "
                    "AND (present = 1 OR :gone = 1) ORDER BY present DESC, rank"
                ),
                {"t": self._tenant, "gone": int(include_gone)},
            ).mappings()
            return [_view(r) for r in rows]

    def decide(self, rid: str, decision: RequestDecision, *, actor: str) -> RequestView:
        if decision not in ("approved", "declined", "open"):
            raise DomainValidationError(f"unknown decision {decision!r}")  # noqa: TRY003
        now = int(self._clock.now().timestamp())
        with self._uow:
            if self._status(rid) == "bound":
                raise DomainValidationError(  # noqa: TRY003
                    f"request {rid} has a bound credential: unbind it first"
                )
            changed = self._uow.connection.execute(
                sa.text(
                    "UPDATE onboarding_requests SET status = :d, decided_by = :a, "
                    "decided_ts = :now WHERE id = :id AND tenant_id = :t AND present = 1"
                ),
                {
                    "d": decision,
                    "a": actor if decision != "open" else None,
                    "now": now,
                    "id": rid,
                    "t": self._tenant,
                },
            ).rowcount
            if not changed:
                raise NotFound(f"request {rid}")  # noqa: TRY003
            if self._audit is not None:
                row = self._uow.connection.execute(
                    sa.text(
                        "SELECT plugin, access, target FROM onboarding_requests "
                        "WHERE id = :id AND tenant_id = :t"
                    ),
                    {"id": rid, "t": self._tenant},
                ).first()
                self._audit.record(
                    "access_decided",
                    actor=actor,
                    detail={
                        "request": "/".join(str(x) for x in row) if row else rid,
                        "decision": decision,
                    },
                )
            self._uow.commit()
        return next(r for r in self.requests() if r.id == rid)

    def get(self, rid: str) -> RequestView:
        found = next((r for r in self.requests() if r.id == rid), None)
        if found is None:
            raise NotFound(f"request {rid}")  # noqa: TRY003
        return found

    def record_verification(
        self, rid: str, *, bound: bool, actor: str, verification: dict[str, object]
    ) -> RequestView:
        """ADR-0019: the probes' results, and the verdict. ``bound`` binds the request;
        a refusal reopens it (the decision is undone: a person must look again)."""
        status: RequestStatus = "bound" if bound else "open"
        self._set(rid, status, actor if bound else None, verification)
        return self.get(rid)

    def mark_unbound(self, rid: str, *, actor: str, why: str) -> RequestView:
        """The credential left the vault: back to ``approved`` (the intent stands)."""
        self._set(rid, "approved", actor, None, action=("access_unbound", why))
        return self.get(rid)

    def _status(self, rid: str) -> str | None:
        row = self._uow.connection.execute(
            sa.text("SELECT status FROM onboarding_requests WHERE id = :id AND tenant_id = :t"),
            {"id": rid, "t": self._tenant},
        ).first()
        return None if row is None else str(row[0])

    def _set(
        self,
        rid: str,
        status: RequestStatus,
        actor: str | None,
        verification: dict[str, object] | None,
        *,
        action: tuple[Literal["access_unbound"], str] | None = None,
    ) -> None:
        now = int(self._clock.now().timestamp())
        with self._uow:
            conn = self._uow.connection
            params: dict[str, object] = {
                "s": status,
                "a": actor,
                "now": now,
                "id": rid,
                "t": self._tenant,
                "v": None if verification is None else json.dumps(verification, sort_keys=True),
                # A request the plan dropped can still lose its credential.
                "any": int(action is not None),
            }
            changed = conn.execute(
                sa.text(
                    "UPDATE onboarding_requests SET status = :s, decided_by = :a, "
                    "decided_ts = :now, verification_json = :v "
                    "WHERE id = :id AND tenant_id = :t AND (present = 1 OR :any = 1)"
                ),
                params,
            ).rowcount
            if not changed:
                raise NotFound(f"request {rid}")  # noqa: TRY003
            if self._audit is not None:
                row = conn.execute(
                    sa.text(
                        "SELECT plugin, access, target FROM onboarding_requests "
                        "WHERE id = :id AND tenant_id = :t"
                    ),
                    {"id": rid, "t": self._tenant},
                ).first()
                name = "/".join(str(x) for x in row) if row else rid
                if action is not None:
                    self._audit.record(
                        action[0],
                        actor=actor or "system",
                        detail={"request": name, "why": action[1]},
                    )
                elif verification is not None:
                    self._audit.record(
                        "access_verified",
                        actor=actor or str(verification.get("by") or "system"),
                        status="ok" if verification.get("bound") else "error",
                        detail={"request": name} | verification,
                    )
            self._uow.commit()

    def progress(self) -> dict[str, int]:
        counts = {"open": 0, "approved": 0, "declined": 0, "bound": 0}
        for r in self.requests():
            counts[r.status] = counts.get(r.status, 0) + 1
        return counts


def _view(row: sa.RowMapping) -> RequestView:
    detail = json.loads(str(row["detail_json"]))

    def strings(key: str) -> tuple[str, ...]:
        return tuple(str(x) for x in detail.get(key, []))

    return RequestView(
        id=str(row["id"]),
        plugin=str(row["plugin"]),
        access=str(row["access"]),
        target=str(row["target"]),
        scope=str(detail["scope"]),
        description=str(detail["description"]),
        found_in=str(detail["found_in"]),
        why=str(detail["why"]),
        unlocks=strings("unlocks"),
        share=float(row["share"]),
        verify_can=strings("verify_can"),
        verify_cannot=strings("verify_cannot"),
        status=str(row["status"]),
        present=bool(row["present"]),
        decided_by=str(row["decided_by"]) if row["decided_by"] else None,
        verification=json.loads(str(row["verification_json"]))
        if row.get("verification_json")
        else None,
    )


__all__ = ["RequestDecision", "RequestReview", "RequestStatus", "RequestView", "request_id"]
