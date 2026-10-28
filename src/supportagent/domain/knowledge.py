"""Knowledge (ADR-0011 §4, WP-M08): documentation as guidance, never evidence.

A **knowledge source** (a Git markdown folder first; Confluence or an MCP
server later) is ingested into :class:`DocChunk` s: redacted *before* they are
chunked and hashed (INV-06), split by heading, each carrying its provenance
(doc id, version, URL, owner, last-updated, content hash) and a
**visibility** that follows the doc.

The rules this module and the RCA service hold to:

- **Guidance, never evidence.** A chunk may *suggest* hypotheses (its doc's
  ``hypotheses:`` front matter): their registered probes join the plan, and
  the tools decide. A claim backed only by a doc is tagged
  ``documented_guidance`` (and ``speculation``), so it is never SUPPORTED and
  never sendable (``test_doc_only_claim_labelled_guidance_not_supported``).
- **Never authority.** Doc text is never executed: a suggestion names a
  taxonomy hypothesis id, nothing else.
- **Scoped.** A search sees only the spaces the case's active packs
  (``knowledge.spaces``) and its catalog service (``docs:``) name, and a doc
  that lists ``services:`` only for those services.
- **Visibility.** ``internal`` (the default) chunks never answer a
  ``customer`` audience (``test_internal_doc_never_in_customer_draft``).
- **Stale docs** (older than the configured age) are still shown, labelled
  ``stale``, but suggest nothing.
- **Cited and replayable.** Every search records the chunk ids and content
  hashes it returned (``test_trace_records_chunk_ids_and_hashes``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from supportagent.domain.memory import bm25
from supportagent.domain.sop import Sop, SopMatch

Visibility = Literal["internal", "customer"]
Audience = Literal["internal", "customer"]

#: A chunk longer than this is split at paragraph breaks.
MAX_CHUNK_CHARS = 1200
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True, slots=True)
class DocMeta:
    """A doc's provenance and scope (front matter, or the source's API)."""

    doc_id: str
    space: str
    title: str
    url: str | None = None
    owner: str | None = None
    version: str | None = None
    last_updated: date | None = None
    visibility: Visibility = "internal"
    services: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    #: Taxonomy hypothesis ids the doc suggests testing (never a verdict).
    hypotheses: tuple[str, ...] = ()
    #: ADR-0014: the doc's SOP block, if it is a procedure.
    sop: Sop | None = None


@dataclass(frozen=True, slots=True)
class DocChunk:
    id: str  # "<doc_id>#<n>"
    meta: DocMeta
    heading: str
    text: str  # redacted
    content_hash: str  # sha256 of the redacted text

    @property
    def doc_id(self) -> str:
        return self.meta.doc_id

    def ref(self) -> dict[str, object]:
        """What a trace and an artifact record about the chunk (never its text)."""
        meta = self.meta
        return {
            "chunk": self.id,
            "doc": meta.doc_id,
            "title": meta.title,
            "heading": self.heading,
            "version": meta.version,
            "url": meta.url,
            "owner": meta.owner,
            "last_updated": meta.last_updated.isoformat() if meta.last_updated else None,
            "visibility": meta.visibility,
            "hash": self.content_hash,
        }


def chunk_document(meta: DocMeta, body: str, *, redact: Callable[[str], str]) -> list[DocChunk]:
    """``body`` redacted (INV-06), then split by ``#``-``###`` headings, then by size."""
    clean = redact(body)
    sections: list[tuple[str, list[str]]] = [(meta.title, [])]
    for line in clean.splitlines():
        found = _HEADING.match(line)
        if found is not None:
            sections.append((found.group(2), []))
        else:
            sections[-1][1].append(line)
    chunks: list[DocChunk] = []
    for heading, lines in sections:
        text = "\n".join(lines).strip()
        if not text:
            continue
        for piece in _split(text):
            digest = hashlib.sha256(piece.encode("utf-8")).hexdigest()
            chunks.append(DocChunk(f"{meta.doc_id}#{len(chunks)}", meta, heading, piece, digest))
    return chunks


def _split(text: str) -> list[str]:
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    out: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        if current and len(current) + len(para) + 2 > MAX_CHUNK_CHARS:
            out.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
        while len(current) > MAX_CHUNK_CHARS:
            out.append(current[:MAX_CHUNK_CHARS])
            current = current[MAX_CHUNK_CHARS:]
    if current:
        out.append(current)
    return out


@dataclass(frozen=True, slots=True)
class DocScope:
    """Where one search may look, and for whom."""

    spaces: frozenset[str]
    service: str | None
    audience: Audience = "internal"


@dataclass(frozen=True, slots=True)
class DocHit:
    chunk: DocChunk
    score: float
    stale: bool

    def ref(self) -> dict[str, object]:
        return {**self.chunk.ref(), "score": round(self.score, 3), "stale": self.stale}


@dataclass(frozen=True, slots=True)
class KnowledgeIndex:
    """BM25 over the ingested chunks (hybrid with local embeddings comes later)."""

    chunks: Sequence[DocChunk]
    stale_after_days: int = 365
    _texts: Mapping[str, str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        ids = [c.id for c in self.chunks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate chunk ids in the knowledge index")  # noqa: TRY003
        texts = {c.id: f"{c.meta.title} {c.heading} {c.text}" for c in self.chunks}
        object.__setattr__(self, "_texts", texts)

    def search(self, query: str, scope: DocScope, *, k: int, today: date) -> list[DocHit]:
        eligible = {c.id: c for c in self.chunks if _in_scope(c, scope)}
        scores = bm25(query, {cid: self._texts[cid] for cid in eligible})
        ranked = sorted(
            (cid for cid, s in scores.items() if s > 0), key=lambda cid: (-scores[cid], cid)
        )
        hits: list[DocHit] = []
        seen_docs: set[str] = set()
        for cid in ranked:
            chunk = eligible[cid]
            if chunk.doc_id in seen_docs:  # one chunk per doc: the best one
                continue
            seen_docs.add(chunk.doc_id)
            hits.append(DocHit(chunk, scores[cid], self.is_stale(chunk, today)))
            if len(hits) >= k:
                break
        return hits

    def sops_for(self, hypothesis: str, scope: DocScope, *, today: date) -> list[SopMatch]:
        """ADR-0014: the steps of fresh, in-scope SOPs whose ``when`` names ``hypothesis``."""
        out: list[SopMatch] = []
        seen: set[str] = set()
        for chunk in self.chunks:
            meta, sop = chunk.meta, chunk.meta.sop
            if sop is None or meta.doc_id in seen or hypothesis not in sop.when:
                continue
            seen.add(meta.doc_id)
            if not _in_scope(chunk, scope) or self.is_stale(chunk, today):
                continue
            out.extend(
                SopMatch(meta.doc_id, meta.title, meta.version, meta.url, sop.digest, hypothesis, s)
                for s in sop.steps
            )
        return out

    def sop_docs(self, *, today: date) -> list[tuple[DocMeta, Sop, bool]]:
        """ADR-0014 part 2: each SOP doc once, with its block and whether it is stale."""
        out: list[tuple[DocMeta, Sop, bool]] = []
        seen: set[str] = set()
        for chunk in self.chunks:
            meta, sop = chunk.meta, chunk.meta.sop
            if sop is not None and meta.doc_id not in seen:
                seen.add(meta.doc_id)
                out.append((meta, sop, self.is_stale(chunk, today)))
        return out

    def is_stale(self, chunk: DocChunk, today: date) -> bool:
        updated = chunk.meta.last_updated
        return updated is not None and (today - updated).days > self.stale_after_days


def _in_scope(chunk: DocChunk, scope: DocScope) -> bool:
    meta = chunk.meta
    if meta.space not in scope.spaces:
        return False
    if scope.audience == "customer" and meta.visibility != "customer":
        return False
    return not meta.services or (scope.service is not None and scope.service in meta.services)


def suggested_hypotheses(hits: Sequence[DocHit], allowed: Collection[str]) -> list[str]:
    """The hypotheses fresh hits suggest, in hit order, limited to ``allowed`` ids."""
    out: list[str] = []
    for hit in hits:
        if hit.stale:
            continue
        for hypothesis in hit.chunk.meta.hypotheses:
            if hypothesis in allowed and hypothesis not in out:
                out.append(hypothesis)
    return out


def guidance_text(hits: Sequence[DocHit], *, max_chars: int) -> str:
    """The hits as planner context (untrusted; the prompt fences it)."""
    parts: list[str] = []
    used = 0
    for hit in hits:
        label = " (stale)" if hit.stale else ""
        block = f"[{hit.chunk.id}] {hit.chunk.meta.title} — {hit.chunk.heading}{label}\n"
        block += hit.chunk.text
        if used + len(block) > max_chars:
            block = block[: max(0, max_chars - used)]
        if not block:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


__all__ = [
    "MAX_CHUNK_CHARS",
    "Audience",
    "DocChunk",
    "DocHit",
    "DocMeta",
    "DocScope",
    "KnowledgeIndex",
    "Visibility",
    "chunk_document",
    "guidance_text",
    "suggested_hypotheses",
]
