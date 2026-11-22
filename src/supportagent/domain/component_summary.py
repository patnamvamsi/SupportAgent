"""Component summaries (ADR-0023 §3, WP-222): what a component does, in a few cited sentences.

The ``summarize_component`` slot reads one component's facts, numbered, and the
code lines those facts cite. Its answer is grounded here before anything is kept:

- a **sentence** is kept only when it cites at least one fact by number and
  every number it cites exists; the model summarises, it doesn't invent;
- an **inferred fact** (a reference static analysis left open, such as a topic
  held in a variable) is kept only when its quote is part of a cited code line,
  its kind is in the vocabulary and its target is already a node of the graph.
  It joins the graph as a layer-3 fact carrying its quote and line, proposed
  for review like every other fact: the model relates, it never discovers.

A brief's :attr:`Brief.digest` covers everything the model is shown, so an
unchanged component is never summarised twice. Pure.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from supportagent.domain.graph import EDGE_KINDS, Edge, EdgeFact, Node

#: How a summary and an inferred fact are attributed.
EXTRACTOR = "summarize_component"
#: The edge kinds a summary may infer: what a component touches or calls.
INFERABLE = frozenset({"publishes", "consumes", "reads", "writes", "uses", "calls"}) & EDGE_KINDS
_CITE = re.compile(r"^git:(?P<path>.+):(?P<line>\d+)$")
_MIN_QUOTE = 8


@dataclass(frozen=True, slots=True)
class CodeLine:
    ref: str  # git:<path>:<line>
    text: str


@dataclass(frozen=True, slots=True)
class Brief:
    """What the model is shown about one component."""

    component: str
    kind: str
    #: ``(number, sentence, its citations)``, numbered from 1.
    facts: tuple[tuple[int, str, tuple[str, ...]], ...]
    code: tuple[CodeLine, ...]
    #: ``(kind, dst)`` of the component's own outgoing edges (never inferred again).
    outgoing: frozenset[tuple[str, str]] = frozenset()

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            "\n".join([self.component, self.kind, facts_text(self), code_text(self)]).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class Sentence:
    text: str
    cites: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Inferred:
    kind: str
    dst: str
    quote: str
    source: str  # the code line the quote is from

    def fact(self, component: str) -> EdgeFact:
        return EdgeFact(component, self.kind, self.dst, self.source, 3, EXTRACTOR, quote=self.quote)


@dataclass(frozen=True, slots=True)
class Summary:
    component: str
    digest: str
    sentences: tuple[Sentence, ...]
    #: Each cited fact's sentence, by number (so a page can show what a sentence rests on).
    facts: Mapping[int, str]
    inferred: tuple[Inferred, ...] = ()
    dropped: int = 0


def brief(
    node: Node,
    edges: Iterable[Edge],
    files: Mapping[str, str],
    *,
    context: int = 1,
    max_lines: int = 80,
) -> Brief:
    """``node``'s facts (the edges it is on, either end) and the code lines they cite."""
    own = sorted(
        (e for e in edges if node.id in (e.src, e.dst)), key=lambda e: (e.kind, e.src, e.dst)
    )
    numbered = []
    for n, edge in enumerate(own, start=1):
        text = f"{edge.src} {edge.kind.replace('_', ' ')} {edge.dst}"
        numbered.append((n, text, tuple(sorted(edge.sources))))
    lines: dict[tuple[str, int], str] = {}
    for _, _, refs in numbered:
        for ref in refs:
            cited = _CITE.match(ref)
            body = files.get(cited["path"]) if cited else None
            if cited is None or body is None:
                continue
            source = body.splitlines()
            at = int(cited["line"])
            for i in range(max(1, at - context), min(len(source), at + context) + 1):
                if len(lines) >= max_lines:
                    break
                lines.setdefault((cited["path"], i), source[i - 1].rstrip())
    code = tuple(CodeLine(f"git:{path}:{i}", text) for (path, i), text in sorted(lines.items()))
    tech = f" ({', '.join(sorted(node.tech))})" if node.tech else ""
    outgoing = frozenset((e.kind, e.dst) for e in own if e.src == node.id)
    return Brief(f"{node.id}{tech}", node.kind, tuple(numbered), code, outgoing)


def facts_text(b: Brief) -> str:
    return "\n".join(f"F{n}: {text} [{', '.join(refs[:3])}]" for n, text, refs in b.facts)


def code_text(b: Brief) -> str:
    return "\n".join(f"{line.ref} | {line.text}" for line in b.code if line.text.strip())


def ground(
    component: str,
    b: Brief,
    sentences: Sequence[tuple[str, Sequence[str | int]]],
    inferred: Sequence[tuple[str, str, str]],
    known: Iterable[str],
) -> Summary:
    """Keep what the brief supports: cited sentences, and inferred facts quoting its code."""
    numbers = {n for n, _, _ in b.facts}
    kept: list[Sentence] = []
    dropped = 0
    for text, cites in sentences:
        parsed = tuple(sorted({int(m) for c in cites for m in re.findall(r"\d+", str(c))}))
        if not text.strip() or not parsed or not set(parsed) <= numbers:
            dropped += 1
            continue
        kept.append(Sentence(" ".join(text.split()), parsed))
    ids = set(known)
    found: dict[tuple[str, str], Inferred] = {}
    for kind, dst, quote in inferred:
        line = _quoted(b, quote)
        if (
            kind not in INFERABLE
            or dst not in ids
            or dst == component
            or line is None
            or (kind, dst) in b.outgoing
        ):
            dropped += 1
            continue
        found.setdefault((kind, dst), Inferred(kind, dst, " ".join(quote.split()), line.ref))
    cited = {n for s in kept for n in s.cites}
    return Summary(
        component=component,
        digest=b.digest,
        sentences=tuple(kept),
        facts={n: text for n, text, _ in b.facts if n in cited},
        inferred=tuple(found.values()),
        dropped=dropped,
    )


def _quoted(b: Brief, quote: str) -> CodeLine | None:
    """The cited code line ``quote`` is from (whitespace aside), or ``None``."""
    wanted = " ".join(quote.split())
    if len(wanted) < _MIN_QUOTE:
        return None
    for line in b.code:
        if wanted in " ".join(line.text.split()):
            return line
    return None


__all__ = [
    "EXTRACTOR",
    "INFERABLE",
    "Brief",
    "CodeLine",
    "Inferred",
    "Sentence",
    "Summary",
    "brief",
    "code_text",
    "facts_text",
    "ground",
]
