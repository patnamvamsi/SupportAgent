"""CaseMemory (ARCH §5.2, §5.3; CAP §4.6; 11-wp WP-108).

- **``remember(case_id, evt, …)``** runs in the triage unit of work for a
  new case. It stores the case node (its frontmatter as JSON), the case's
  text **redacted** (the same detectors as the LLM boundary, INV-06) and
  **normalized** (01 §7.2), and its normalized error signature as a
  signature node. ``similar`` edges to every case with that signature are
  *derived* here (ARCH §5.3), never asserted.
- **``index_resolved(case_id)``** makes the case retrievable: only resolved
  cases are "past cases".
- **``similar(case_id, k=3)``** ranks resolved cases with ``domain.memory.rank``.
  It returns nothing when nothing matches.

Every query is tenant-scoped (``test_memory_tenant_isolated``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa

from supportagent.domain.memory import DEFAULT_WEIGHTS, Citation, MemoryDoc, rank
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.normalize import normalize
from supportagent.ports.clock import Clock


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


_UPSERT_NODE = (
    "INSERT INTO nodes (id, tenant_id, kind, ref, label, updated_ts) "
    "VALUES (:id, :t, :kind, :ref, :label, :ts) "
    "ON CONFLICT (tenant_id, kind, ref) DO UPDATE SET label = excluded.label, "
    "updated_ts = excluded.updated_ts"
)
_INSERT_EDGE = (
    "INSERT INTO edges (src, dst, tenant_id, kind, weight, created_ts) "
    "VALUES (:src, :dst, :t, :kind, 1, :ts) ON CONFLICT (src, dst, kind) DO NOTHING"
)


class CaseMemory:
    def __init__(  # noqa: PLR0913 — the ports plus the text pipeline and weights
        self,
        *,
        uow: _UoW,
        clock: Clock,
        tenant_id: str,
        redact: Callable[[str], str],
        tenant_constants: Sequence[str] = (),
        weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._tenant = tenant_id
        self._redact = redact
        self._consts = tuple(tenant_constants)
        self._weights = dict(weights)

    # --- write -----------------------------------------------------------------

    def remember(
        self,
        case_id: str,
        evt: AlertEvent,
        *,
        service: str | None,
        classification: str | None,
        severity: str | None,
    ) -> None:
        now_ts = int(self._clock.now().timestamp())
        signature = normalize(evt.error_signature, self._consts) if evt.error_signature else None
        text = normalize(self._redact(f"{evt.subject}\n{evt.body}"), self._consts)
        label = {
            "service": service,
            "classification": classification,
            "severity": severity,
            "signature": signature,
            "resolved": False,
            "opened_ts": now_ts,
        }
        with self._uow:
            conn = self._uow.connection
            node = self._node(conn, "case", case_id, label, now_ts)
            conn.execute(
                sa.text(
                    "INSERT INTO kb_docs (tenant_id, ref, text, updated_ts) VALUES "
                    "(:t, :ref, :text, :ts) ON CONFLICT (tenant_id, ref) DO UPDATE SET "
                    "text = excluded.text, updated_ts = excluded.updated_ts"
                ),
                {"t": self._tenant, "ref": case_id, "text": text, "ts": now_ts},
            )
            if signature:
                sig = self._node(conn, "signature", signature, {"signature": signature}, now_ts)
                self._edge(conn, node, sig, "has_signature", now_ts)
                # ARCH §5.3: ``similar`` is derived from signature equality.
                for other in conn.execute(
                    sa.text(
                        "SELECT src FROM edges WHERE tenant_id = :t AND dst = :sig "
                        "AND kind = 'has_signature' AND src <> :me"
                    ),
                    {"t": self._tenant, "sig": sig, "me": node},
                ).scalars():
                    self._edge(conn, node, str(other), "similar", now_ts)
                    self._edge(conn, str(other), node, "similar", now_ts)
            self._uow.commit()

    def index_resolved(self, case_id: str) -> None:
        """The case is now a past case: refresh its labels from the case row."""
        now_ts = int(self._clock.now().timestamp())
        with self._uow:
            conn = self._uow.connection
            row = conn.execute(
                sa.text(
                    "SELECT n.label, c.service, c.classification, c.severity FROM nodes n "
                    "JOIN cases c ON c.id = n.ref WHERE n.tenant_id = :t AND n.kind = 'case' "
                    "AND n.ref = :c"
                ),
                {"t": self._tenant, "c": case_id},
            ).first()
            if row is None:
                return
            label = json.loads(str(row[0]))
            label.update(
                resolved=True,
                service=row[1] or label.get("service"),
                classification=row[2] or label.get("classification"),
                severity=row[3] or label.get("severity"),
            )
            self._node(conn, "case", case_id, label, now_ts)
            self._uow.commit()

    # --- read ------------------------------------------------------------------

    def similar(self, case_id: str, *, k: int = 3) -> list[Citation]:
        with self._uow:
            conn = self._uow.connection
            docs = self._docs(conn)
        query = docs.get(case_id)
        if query is None:
            return []
        candidates = [d for cid, (d, resolved) in docs.items() if resolved and cid != case_id]
        return rank(query[0], candidates, k=k, weights=self._weights)

    def exists(self, case_id: str) -> bool:
        with self._uow:
            return (
                self._uow.connection.execute(
                    sa.text("SELECT 1 FROM cases WHERE id = :c AND tenant_id = :t"),
                    {"c": case_id, "t": self._tenant},
                ).first()
                is not None
            )

    # --- helpers ---------------------------------------------------------------

    def _docs(self, conn: sa.Connection) -> dict[str, tuple[MemoryDoc, bool]]:
        now_ts = int(self._clock.now().timestamp())
        rows = conn.execute(
            sa.text(
                "SELECT n.id, n.ref, n.label, d.text FROM nodes n LEFT JOIN kb_docs d "
                "ON d.tenant_id = n.tenant_id AND d.ref = n.ref "
                "WHERE n.tenant_id = :t AND n.kind = 'case'"
            ),
            {"t": self._tenant},
        ).all()
        neighbours: dict[str, set[str]] = {}
        for src, dst in conn.execute(
            sa.text(
                "SELECT src, dst FROM edges WHERE tenant_id = :t "
                "AND kind IN ('similar', 'resolved_by')"
            ),
            {"t": self._tenant},
        ).all():
            neighbours.setdefault(str(src), set()).add(str(dst))
        out: dict[str, tuple[MemoryDoc, bool]] = {}
        for node_id, ref, label_json, text in rows:
            label = json.loads(str(label_json))
            opened = int(label.get("opened_ts") or now_ts)
            out[str(ref)] = (
                MemoryDoc(
                    case_id=str(ref),
                    text=str(text or ""),
                    signature=label.get("signature"),
                    service=label.get("service"),
                    classification=label.get("classification"),
                    severity=label.get("severity"),
                    age_days=(now_ts - opened) / 86_400,
                    neighbours=frozenset(neighbours.get(str(node_id), set())),
                ),
                bool(label.get("resolved")),
            )
        return out

    def _node(
        self, conn: sa.Connection, kind: str, ref: str, label: Mapping[str, object], ts: int
    ) -> str:
        node_id = f"{kind}:{self._tenant}:{ref}"
        conn.execute(
            sa.text(_UPSERT_NODE),
            {
                "id": node_id,
                "t": self._tenant,
                "kind": kind,
                "ref": ref,
                "label": json.dumps(dict(label), sort_keys=True),
                "ts": ts,
            },
        )
        return node_id

    def _edge(self, conn: sa.Connection, src: str, dst: str, kind: str, ts: int) -> None:
        conn.execute(
            sa.text(_INSERT_EDGE),
            {"src": src, "dst": dst, "t": self._tenant, "kind": kind, "ts": ts},
        )


__all__ = ["CaseMemory"]
