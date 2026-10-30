"""Facts from prose (ADR-0015 §3 layer 3, WP-O3), and the checks that ground them.

The ``extract_graph`` slot reads one current documentation page and returns
the facts it states, each with a quote. A fact enters the graph only when the
check here passes; there is no "the model said so":

- the quote is in the page, word for word (whitespace, case and Markdown
  emphasis aside);
- the quote names **both** ends of the fact (each entity's name, or all of
  the parts of a dotted name such as ``paydb.orders``);
- each end is a known entity, or one the same answer declares with its own
  grounded quote;
- the kind is in the graph's vocabulary.

Everything that fails is reported with its reason (the review screen shows
rejected facts too).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from supportagent.domain.graph import EDGE_KINDS, EdgeFact, Fact, NodeFact, NodeKind

NODE_KINDS: frozenset[str] = frozenset(
    {"Service", "Job", "UI", "Database", "Datastore", "Topic", "Runtime", "External"}
)
_MAX_QUOTE = 400
_MIN_QUOTE = 8
_ID = re.compile(r"^[a-z]+:[a-z0-9][a-z0-9._/-]*$")


@dataclass(frozen=True, slots=True)
class ProseEntity:
    id: str
    kind: str
    quote: str = ""


@dataclass(frozen=True, slots=True)
class ProseFact:
    src: str
    kind: str
    dst: str
    quote: str = ""


@dataclass(frozen=True, slots=True)
class GraphExtraction:
    """The model's answer for one page, already parsed (``app.graph`` validates the JSON)."""

    entities: tuple[ProseEntity, ...] = ()
    facts: tuple[ProseFact, ...] = ()


@dataclass(frozen=True, slots=True)
class Rejected:
    what: str
    why: str


def normalize(text: str) -> str:
    """Lower case, Markdown emphasis and table bars gone, whitespace collapsed."""
    text = re.sub(r"[`*_|>]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _names(entity_id: str) -> list[str]:
    """What a quote must say for an entity: its name, or each part of a dotted name.

    Hyphens are kept: ``orders-service`` must appear as written, so a phrase
    such as "every service" never grounds an entity ``service:every-service``.
    Underscores read as spaces, as ``normalize`` reads Markdown.
    """
    name = entity_id.split(":", 1)[1].rsplit("/", 1)[-1]
    parts = [p for p in name.split(".") if p] or [name]
    return [p.replace("_", " ") for p in parts]


def _mentions(quote: str, entity_id: str) -> bool:
    words = normalize(quote)
    return all(
        re.search(rf"(?<![a-z0-9-]){re.escape(n)}(?![a-z0-9-])", words) for n in _names(entity_id)
    )


def ground(
    extraction: GraphExtraction,
    *,
    page_id: str,
    page_text: str,
    known: Collection[str],
) -> tuple[list[Fact], list[Rejected]]:
    page = normalize(page_text)
    source = f"confluence:{page_id}"
    facts: list[Fact] = []
    rejected: list[Rejected] = []

    def quoted(quote: str, what: str) -> bool:
        clean = normalize(quote)
        if not _MIN_QUOTE <= len(clean) <= _MAX_QUOTE:
            rejected.append(Rejected(what, "no usable quote"))
            return False
        if clean not in page:
            rejected.append(Rejected(what, "the quote is not in the page"))
            return False
        return True

    declared: set[str] = set()
    for entity in extraction.entities:
        what = f"entity {entity.id}"
        if not _ID.match(entity.id) or entity.kind not in NODE_KINDS:
            rejected.append(Rejected(what, "not an id and kind of the vocabulary"))
            continue
        if entity.id in known:
            continue
        if quoted(entity.quote, what) and _mentions(entity.quote, entity.id):
            declared.add(entity.id)
            kind: NodeKind = entity.kind  # type: ignore[assignment]
            facts.append(NodeFact(entity.id, kind, source, 3, "extract_graph", quote=entity.quote))
        elif entity.id not in {r.what for r in rejected}:
            rejected.append(Rejected(what, "the quote doesn't name it"))
    usable = set(known) | declared
    for fact in extraction.facts:
        what = f"{fact.src} {fact.kind} {fact.dst}"
        if fact.kind not in EDGE_KINDS:
            rejected.append(Rejected(what, f"{fact.kind!r} is not a fact kind"))
            continue
        missing = [e for e in (fact.src, fact.dst) if e not in usable]
        if missing:
            rejected.append(Rejected(what, f"unknown entity {missing[0]}"))
            continue
        if not quoted(fact.quote, what):
            continue
        unnamed = [e for e in (fact.src, fact.dst) if not _mentions(fact.quote, e)]
        if unnamed:
            rejected.append(Rejected(what, f"the quote doesn't name {unnamed[0]}"))
            continue
        facts.append(
            EdgeFact(fact.src, fact.kind, fact.dst, source, 3, "extract_graph", quote=fact.quote)
        )
    return facts, rejected


def known_entities_text(nodes: Sequence[tuple[str, str]]) -> str:
    return "\n".join(f"- {node_id} ({kind})" for node_id, kind in nodes) or "(none yet)"


def chunks(text: str, *, max_chars: int = 8000) -> list[str]:
    """A long page split at headings, each part at most ``max_chars``."""
    if len(text) <= max_chars:
        return [text]
    parts, current = [], ""
    for block in re.split(r"(?m)^(?=#{1,3} )", text):
        if current and len(current) + len(block) > max_chars:
            parts.append(current)
            current = ""
        current += block
    if current:
        parts.append(current)
    return [p[i : i + max_chars] for p in parts for i in range(0, len(p), max_chars)]


__all__ = [
    "NODE_KINDS",
    "GraphExtraction",
    "ProseEntity",
    "ProseFact",
    "Rejected",
    "chunks",
    "ground",
    "known_entities_text",
    "normalize",
]
