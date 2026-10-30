"""Reviewing the system graph (ADR-0015 §3, WP-O3b, decision 4: easy, visual).

Onboarding proposes facts; a subject-matter expert confirms or rejects them.
This service stores a graph build and records the decisions:

- :meth:`save` upserts every node and edge of a build, with its sources
  (links, and the quote for a fact read from documentation). A rebuild keeps
  each fact's review decision; a fact the new build no longer finds stays,
  marked not ``present``, so a decision is never silently lost.
- :meth:`decide` confirms or rejects facts by id; :meth:`decide_layer`
  does it for everything from one layer of trust ("confirm all from code
  and config").
- :meth:`resolve` settles a question by one of its options; an option that
  carries an edge adds it, confirmed by the person who chose it.
- :meth:`progress` is what the overview shows: how much is reviewed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, Protocol, Self

import sqlalchemy as sa

from supportagent.app.graph import GraphBuild
from supportagent.domain.errors import DomainValidationError, NotFound
from supportagent.domain.graph import AliasFact, EdgeFact, NodeFact
from supportagent.ports.clock import Clock

Decision = Literal["confirmed", "rejected", "proposed"]
LAYER_LABELS = {
    0: "a person",
    1: "code, config and the CMDB",
    2: "live discovery",
    3: "documentation",
}


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
class FactView:
    id: str
    fact: Literal["node", "edge"]
    src: str
    kind: str
    dst: str | None
    tech: tuple[str, ...]
    #: Each source: ``ref``, ``url`` (when one is known) and ``quote`` (documentation).
    sources: tuple[Mapping[str, str | None], ...]
    layer: int
    status: str
    present: bool
    reviewed_by: str | None

    @property
    def layer_label(self) -> str:
        return LAYER_LABELS.get(self.layer, str(self.layer))

    @property
    def text(self) -> str:
        if self.fact == "node":
            return f"{self.src} is a {self.kind}"
        return f"{self.src} {self.kind.replace('_', ' ')} {self.dst}"


@dataclass(frozen=True, slots=True)
class QuestionView:
    id: str
    about: str
    detail: str
    sources: tuple[str, ...]
    options: tuple[Mapping[str, object], ...]
    status: str
    choice: int | None
    resolved_by: str | None


def fact_id(fact: str, src: str, kind: str, dst: str | None) -> str:
    key = "|".join([fact, src, kind, dst or ""])
    return hashlib.sha256(key.encode()).hexdigest()[:20]


class GraphReview:
    def __init__(self, *, uow: _UoW, clock: Clock, tenant_id: str) -> None:
        self._uow = uow
        self._clock = clock
        self._tenant = tenant_id

    # -- saving a build ---------------------------------------------------------------

    def save(self, built: GraphBuild, links: Mapping[str, str]) -> dict[str, int]:
        """Upsert a build; ``links`` maps a source ref to a URL (a Confluence page, say)."""
        now = int(self._clock.now().timestamp())
        quotes = _quotes(built)
        rows: list[dict[str, object]] = []
        for node in built.graph.nodes.values():
            rows.append(
                {
                    "fact": "node",
                    "src": node.id,
                    "kind": node.kind,
                    "dst": None,
                    "tech": sorted(node.tech),
                    "sources": _sources(node.sources, links, quotes.get(("node", node.id), {})),
                    "layer": node.layer,
                }
            )
        for edge in built.graph.edges.values():
            rows.append(
                {
                    "fact": "edge",
                    "src": edge.src,
                    "kind": edge.kind,
                    "dst": edge.dst,
                    "tech": [],
                    "sources": _sources(edge.sources, links, quotes.get(edge.key, {})),
                    "layer": edge.layer,
                }
            )
        counts = {"new": 0, "kept": 0, "gone": 0}
        with self._uow:
            conn = self._uow.connection
            seen: set[str] = set()
            for row in rows:
                fid = fact_id(str(row["fact"]), str(row["src"]), str(row["kind"]), row["dst"])  # type: ignore[arg-type]
                seen.add(fid)
                values = {
                    "id": fid,
                    "t": self._tenant,
                    "fact": row["fact"],
                    "src": row["src"],
                    "kind": row["kind"],
                    "dst": row["dst"],
                    "tech": json.dumps(row["tech"]),
                    "sources": json.dumps(row["sources"], sort_keys=True),
                    "layer": row["layer"],
                    "now": now,
                }
                updated = conn.execute(
                    sa.text(
                        "UPDATE graph_facts SET tech_json = :tech, sources_json = :sources, "
                        "layer = :layer, present = 1, updated_ts = :now "
                        "WHERE id = :id AND tenant_id = :t"
                    ),
                    values,
                ).rowcount
                if updated:
                    counts["kept"] += 1
                    continue
                conn.execute(
                    sa.text(
                        "INSERT INTO graph_facts (id, tenant_id, fact, src, kind, dst, tech_json, "
                        "sources_json, layer, status, present, updated_ts) VALUES (:id, :t, "
                        ":fact, :src, :kind, :dst, :tech, :sources, :layer, 'proposed', 1, :now)"
                    ),
                    values,
                )
                counts["new"] += 1
            existing = conn.execute(
                sa.text(
                    "SELECT id FROM graph_facts WHERE tenant_id = :t AND present = 1 AND layer > 0"
                ),
                {"t": self._tenant},
            ).scalars()
            gone = [i for i in existing if i not in seen]
            for gid in gone:
                conn.execute(
                    sa.text("UPDATE graph_facts SET present = 0, updated_ts = :now WHERE id = :id"),
                    {"id": gid, "now": now},
                )
            counts["gone"] = len(gone)
            for question in built.graph.questions:
                qid = hashlib.sha256(question.about.encode()).hexdigest()[:20]
                options = [
                    {"label": o.label, "edge": list(o.edge) if o.edge else None}
                    for o in question.options
                ]
                values = {
                    "id": qid,
                    "t": self._tenant,
                    "about": question.about,
                    "detail": question.detail,
                    "sources": json.dumps(list(question.sources)),
                    "options": json.dumps(options),
                    "now": now,
                }
                if not conn.execute(
                    sa.text(
                        "UPDATE graph_questions SET detail = :detail, sources_json = :sources, "
                        "options_json = :options, updated_ts = :now WHERE id = :id AND tenant_id "
                        "= :t"
                    ),
                    values,
                ).rowcount:
                    conn.execute(
                        sa.text(
                            "INSERT INTO graph_questions (id, tenant_id, about, detail, "
                            "sources_json, "
                            "options_json, status, updated_ts) VALUES (:id, :t, :about, :detail, "
                            ":sources, :options, 'open', :now)"
                        ),
                        values,
                    )
            self._uow.commit()
        return counts

    # -- reading ------------------------------------------------------------------------

    def facts(
        self,
        *,
        status: str | None = None,
        layer: int | None = None,
        focus: str | None = None,
        include_gone: bool = False,
    ) -> list[FactView]:
        sql = "SELECT * FROM graph_facts WHERE tenant_id = :t"
        params: dict[str, object] = {"t": self._tenant}
        if status:
            sql += " AND status = :status"
            params["status"] = status
        if layer is not None:
            sql += " AND layer = :layer"
            params["layer"] = layer
        if focus:
            sql += " AND (src = :focus OR dst = :focus)"
            params["focus"] = focus
        if not include_gone:
            sql += " AND present = 1"
        sql += (
            " ORDER BY CASE status WHEN 'proposed' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END,"
            " layer DESC, fact DESC, src, kind, dst"
        )
        with self._uow:
            rows = self._uow.connection.execute(sa.text(sql), params).mappings().all()
        return [_view(r) for r in rows]

    def questions(self) -> list[QuestionView]:
        with self._uow:
            rows = (
                self._uow.connection.execute(
                    sa.text(
                        "SELECT * FROM graph_questions WHERE tenant_id = :t "
                        "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, about"
                    ),
                    {"t": self._tenant},
                )
                .mappings()
                .all()
            )
        return [
            QuestionView(
                id=str(r["id"]),
                about=str(r["about"]),
                detail=str(r["detail"]),
                sources=tuple(json.loads(r["sources_json"])),
                options=tuple(json.loads(r["options_json"])),
                status=str(r["status"]),
                choice=r["choice"],
                resolved_by=r["resolved_by"],
            )
            for r in rows
        ]

    def progress(self) -> dict[str, object]:
        with self._uow:
            conn = self._uow.connection
            rows = conn.execute(
                sa.text(
                    "SELECT layer, status, COUNT(*) AS n FROM graph_facts "
                    "WHERE tenant_id = :t AND present = 1 GROUP BY layer, status"
                ),
                {"t": self._tenant},
            ).mappings()
            by_layer: dict[int, dict[str, int]] = {}
            for r in rows:
                by_layer.setdefault(int(r["layer"]), {})[str(r["status"])] = int(r["n"])
            open_questions = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM graph_questions WHERE tenant_id = :t AND status = 'open'"
                ),
                {"t": self._tenant},
            ).scalar_one()
        total = sum(sum(v.values()) for v in by_layer.values())
        decided = sum(v.get("confirmed", 0) + v.get("rejected", 0) for v in by_layer.values())
        return {
            "total": total,
            "reviewed": decided,
            "share": round(decided / total, 3) if total else 0.0,
            "confirmed": sum(v.get("confirmed", 0) for v in by_layer.values()),
            "rejected": sum(v.get("rejected", 0) for v in by_layer.values()),
            "by_layer": {
                layer: {"label": LAYER_LABELS.get(layer, str(layer)), **counts}
                for layer, counts in sorted(by_layer.items())
            },
            "open_questions": int(open_questions),
        }

    # -- decisions ------------------------------------------------------------------------

    def decide(self, ids: Sequence[str], decision: Decision, *, actor: str) -> int:
        if decision not in ("confirmed", "rejected", "proposed"):
            raise DomainValidationError(f"unknown decision {decision!r}")  # noqa: TRY003
        if not ids:
            return 0
        now = int(self._clock.now().timestamp())
        with self._uow:
            changed = 0
            for fid in ids:
                changed += self._uow.connection.execute(
                    sa.text(
                        "UPDATE graph_facts SET status = :d, reviewed_by = :a, reviewed_ts = :now "
                        "WHERE id = :id AND tenant_id = :t AND present = 1"
                    ),
                    {
                        "d": decision,
                        "a": actor if decision != "proposed" else None,
                        "now": now,
                        "id": fid,
                        "t": self._tenant,
                    },
                ).rowcount
            self._uow.commit()
        return changed

    def decide_layer(self, layer: int, decision: Decision, *, actor: str) -> int:
        """Every still-proposed fact from one layer at once (``confirm all from code``)."""
        ids = [f.id for f in self.facts(status="proposed", layer=layer)]
        return self.decide(ids, decision, actor=actor)

    def resolve(self, question_id: str, choice: int, *, actor: str) -> None:
        now = int(self._clock.now().timestamp())
        with self._uow:
            conn = self._uow.connection
            row = (
                conn.execute(
                    sa.text(
                        "SELECT options_json FROM graph_questions WHERE id = :id AND tenant_id = :t"
                    ),
                    {"id": question_id, "t": self._tenant},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound(f"question not found: {question_id!r}")  # noqa: TRY003
            options = json.loads(row["options_json"])
            if not 0 <= choice < len(options):
                raise DomainValidationError(f"choice {choice} is not an option")  # noqa: TRY003
            conn.execute(
                sa.text(
                    "UPDATE graph_questions SET status = 'resolved', choice = :c, resolved_by = "
                    ":a, "
                    "resolved_ts = :now WHERE id = :id"
                ),
                {"c": choice, "a": actor, "now": now, "id": question_id},
            )
            edge = options[choice].get("edge")
            if edge:
                src, kind, dst = edge
                fid = fact_id("edge", src, kind, dst)
                sources = json.dumps([{"ref": f"review:{actor}", "url": None, "quote": None}])
                exists = conn.execute(
                    sa.text("SELECT 1 FROM graph_facts WHERE id = :id"), {"id": fid}
                ).first()
                if exists is None:
                    conn.execute(
                        sa.text(
                            "INSERT INTO graph_facts (id, tenant_id, fact, src, kind, dst, "
                            "sources_json, "
                            "layer, status, present, reviewed_by, reviewed_ts, updated_ts) VALUES "
                            "(:id, :t, 'edge', :src, :kind, :dst, :sources, 0, 'confirmed', 1, "
                            ":a, :now, :now)"
                        ),
                        {
                            "id": fid,
                            "t": self._tenant,
                            "src": src,
                            "kind": kind,
                            "dst": dst,
                            "sources": sources,
                            "a": actor,
                            "now": now,
                        },
                    )
                else:
                    conn.execute(
                        sa.text(
                            "UPDATE graph_facts SET status = 'confirmed', present = 1, "
                            "reviewed_by = :a, "
                            "reviewed_ts = :now WHERE id = :id"
                        ),
                        {"id": fid, "a": actor, "now": now},
                    )
            self._uow.commit()


def _quotes(built: GraphBuild) -> dict[object, dict[str, str]]:
    """Per fact key, each documentation source's quote."""
    out: dict[object, dict[str, str]] = {}
    for fact in built.facts:
        if isinstance(fact, AliasFact) or fact.quote is None:
            continue
        key: object = (
            ("node", fact.id) if isinstance(fact, NodeFact) else (fact.src, fact.kind, fact.dst)
        )
        if isinstance(fact, EdgeFact | NodeFact):
            out.setdefault(key, {})[fact.source] = fact.quote
    return out


def _sources(
    refs: set[str], links: Mapping[str, str], quotes: Mapping[str, str]
) -> list[dict[str, str | None]]:
    return [{"ref": r, "url": links.get(r), "quote": quotes.get(r)} for r in sorted(refs)]


def _view(r: sa.RowMapping) -> FactView:
    return FactView(
        id=str(r["id"]),
        fact="node" if r["fact"] == "node" else "edge",
        src=str(r["src"]),
        kind=str(r["kind"]),
        dst=None if r["dst"] is None else str(r["dst"]),
        tech=tuple(json.loads(str(r["tech_json"]))),
        sources=tuple(json.loads(str(r["sources_json"]))),
        layer=int(r["layer"]),
        status=str(r["status"]),
        present=bool(r["present"]),
        reviewed_by=None if r["reviewed_by"] is None else str(r["reviewed_by"]),
    )


__all__ = ["LAYER_LABELS", "FactView", "GraphReview", "QuestionView", "fact_id"]
