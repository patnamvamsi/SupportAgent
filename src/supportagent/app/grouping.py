"""GroupingService: storm grouping's I/O (ARCH §4.3, 01 §7.8; 11-wp WP-111).

``assign`` runs inside the triage unit of work for each **new** case. It
records the case's source, loads the tenant's open groups and the 5-minute
new-case count, applies ``domain.grouping.assign_group``, and links the case:
it joins a group (``member_count + 1``) or opens one with this case as its
lead. ``close_due`` is the ``groups.close`` job: it closes groups whose
members are all resolved, or that have had no new member for 24 h.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa

from supportagent.domain.grouping import (
    STORM_WINDOW_S,
    WINDOW_S,
    GroupDecision,
    GroupMember,
    OpenGroup,
    assign_group,
    should_close,
)
from supportagent.domain.ids import IdPrefix
from supportagent.domain.model.catalog import Catalog
from supportagent.ports.idgen import IdGenerator


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
class GroupOutcome:
    decision: GroupDecision
    group_id: str | None
    lead_case_id: str | None
    #: The lead's ``decision_json`` (parsed), for a storm member's suggestion.
    lead_decision: dict[str, object] | None


class GroupingService:
    def __init__(self, *, uow: _UoW, ids: IdGenerator, catalog: Catalog, tenant_id: str) -> None:
        self._uow = uow
        self._ids = ids
        self._catalog = catalog
        self._tenant = tenant_id

    def assign(
        self, case_id: str, *, service: str | None, source: str, now: datetime
    ) -> GroupOutcome:
        now_ts = int(now.timestamp())
        with self._uow:
            conn = self._uow.connection
            conn.execute(
                sa.text("UPDATE cases SET source = :src WHERE id = :id"),
                {"src": source, "id": case_id},
            )
            groups = self._open_groups(conn, since_ts=now_ts - WINDOW_S)
            recent = int(
                conn.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM cases WHERE tenant_id = :t AND opened_ts >= :since "
                        "AND id <> :id"
                    ),
                    {"t": self._tenant, "since": now_ts - STORM_WINDOW_S, "id": case_id},
                ).scalar_one()
            )
            decision = assign_group(
                service=service,
                source=source,
                now_ts=now_ts,
                open_groups=groups,
                recent_new_cases=recent,
                catalog=self._catalog,
            )
            if decision.action == "join" and decision.group_id is not None:
                group = next(g for g in groups if g.id == decision.group_id)
                self._link(conn, case_id, group.id)
                return GroupOutcome(
                    decision,
                    group.id,
                    group.lead_case_id,
                    self._decision_of(conn, group.lead_case_id),
                )
            if decision.action == "open":
                group_id = self._ids.new(IdPrefix.GROUP)
                conn.execute(
                    sa.text(
                        "INSERT INTO case_groups (id, tenant_id, opened_ts, rule, member_count) "
                        "VALUES (:id, :t, :ts, 'storm', 0)"
                    ),
                    {"id": group_id, "t": self._tenant, "ts": now_ts},
                )
                self._link(conn, case_id, group_id)
                return GroupOutcome(decision, group_id, case_id, None)
        return GroupOutcome(decision, None, None, None)

    def close_due(self, now: datetime) -> int:
        """``groups.close`` (02 §7, 5 min): close resolved or idle groups."""
        now_ts = int(now.timestamp())
        closed = 0
        with self._uow:
            conn = self._uow.connection
            for group in self._open_groups(conn, since_ts=None):
                if should_close(group, now_ts):
                    conn.execute(
                        sa.text("UPDATE case_groups SET closed_ts = :ts WHERE id = :id"),
                        {"ts": now_ts, "id": group.id},
                    )
                    closed += 1
            self._uow.commit()
        return closed

    # --- helpers --------------------------------------------------------------

    def _link(self, conn: sa.Connection, case_id: str, group_id: str) -> None:
        conn.execute(
            sa.text("UPDATE cases SET group_id = :g WHERE id = :id"), {"g": group_id, "id": case_id}
        )
        conn.execute(
            sa.text("UPDATE case_groups SET member_count = member_count + 1 WHERE id = :g"),
            {"g": group_id},
        )

    def _open_groups(self, conn: sa.Connection, *, since_ts: int | None) -> list[OpenGroup]:
        sql = "SELECT id, opened_ts FROM case_groups WHERE tenant_id = :t AND closed_ts IS NULL"
        params: dict[str, object] = {"t": self._tenant}
        if since_ts is not None:
            sql += " AND opened_ts >= :since"
            params["since"] = since_ts
        groups: list[OpenGroup] = []
        for gid, opened in conn.execute(sa.text(sql + " ORDER BY opened_ts, id"), params).all():
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, service, source, status, opened_ts FROM cases "
                        "WHERE group_id = :g ORDER BY opened_ts, id"
                    ),
                    {"g": gid},
                )
                .mappings()
                .all()
            )
            members = [
                GroupMember(
                    case_id=str(r["id"]),
                    service=r["service"],
                    source=str(r["source"] or ""),
                    resolved=r["status"] == "resolved",
                )
                for r in rows
            ]
            groups.append(
                OpenGroup(
                    id=str(gid),
                    opened_ts=int(opened),
                    lead_case_id=members[0].case_id if members else "",
                    members=members,
                    last_member_ts=max((int(r["opened_ts"]) for r in rows), default=int(opened)),
                )
            )
        return groups

    @staticmethod
    def _decision_of(conn: sa.Connection, case_id: str) -> dict[str, object] | None:
        raw = conn.execute(
            sa.text("SELECT decision_json FROM cases WHERE id = :id"), {"id": case_id}
        ).scalar()
        return json.loads(raw) if raw else None


__all__ = ["GroupOutcome", "GroupingService"]
