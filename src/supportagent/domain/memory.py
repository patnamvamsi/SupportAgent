"""Case-memory ranking (ARCH §5.2, 11-wp WP-108): every component defined.

``score = Σ wᵢ · componentᵢ`` over resolved past cases, each component in [0, 1]:

- **``signature_match``**: 1.0 when the normalized error signatures are equal;
  0.5 when they share their first ``:``-separated segment (the same error
  family, e.g. ``5xx``); else 0. This is the strongest signal.
- **``bm25``**: Okapi BM25 (k1 = 1.2, b = 0.75) of the query case's
  normalized text against each candidate, divided by the best candidate's score.
- **``structured_match``**: 0.5 for the same service, 0.3 for the same
  classification domain, 0.2 for the same severity.
- **``graph_similarity``**: cosine of the two cases' neighbour sets over
  ``similar`` and ``resolved_by`` edges.
- **``recency``**: ``exp(-age_days / 30)``.

**Honesty (ARCH §5.2):** a candidate counts only if it matches on the
signature or the text. Service-level similarity alone is not a "similar
case". No matches means an empty result; nothing is padded or invented.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

DEFAULT_WEIGHTS: Mapping[str, float] = {
    "signature_match": 0.40,
    "bm25": 0.20,
    "structured_match": 0.20,
    "graph_similarity": 0.10,
    "recency": 0.10,
}
_K1 = 1.2
_B = 0.75
_TOKEN = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class MemoryDoc:
    """One remembered case, as the ranker sees it."""

    case_id: str
    text: str
    signature: str | None
    service: str | None
    classification: str | None
    severity: str | None
    age_days: float
    neighbours: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Citation:
    """A similar past case, with its score and why (01 §3 citation IDs)."""

    case_id: str
    score: float
    components: Mapping[str, float] = field(default_factory=dict)


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def signature_match(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return 0.5 if a.split(":", 1)[0] == b.split(":", 1)[0] else 0.0


def structured_match(query: MemoryDoc, doc: MemoryDoc) -> float:
    score = 0.0
    if query.service and query.service == doc.service:
        score += 0.5
    if (
        query.classification
        and doc.classification
        and (query.classification.split("/", 1)[0] == doc.classification.split("/", 1)[0])
    ):
        score += 0.3
    if query.severity and query.severity == doc.severity:
        score += 0.2
    return score


def graph_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


def bm25_scores(query: str, docs: Sequence[MemoryDoc]) -> dict[str, float]:
    """Raw BM25 per candidate (0.0 when no query term occurs)."""
    return bm25(query, {d.case_id: d.text for d in docs})


def bm25(query: str, texts: Mapping[str, str]) -> dict[str, float]:
    """Raw BM25 of ``query`` against each text, by id (case memory and knowledge, WP-M08)."""
    if not texts:
        return {}
    bags = {key: Counter(tokens(text)) for key, text in texts.items()}
    lengths = {k: sum(v.values()) for k, v in bags.items()}
    avg = (sum(lengths.values()) / len(lengths)) or 1.0
    n = len(texts)
    terms = set(tokens(query))
    df = {t: sum(1 for bag in bags.values() if t in bag) for t in terms}
    scores: dict[str, float] = {}
    for key, bag in bags.items():
        s = 0.0
        for t in terms:
            f = bag.get(t, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * (_K1 + 1) / (f + _K1 * (1 - _B + _B * lengths[key] / avg))
        scores[key] = s
    return scores


def rank(
    query: MemoryDoc,
    candidates: Sequence[MemoryDoc],
    *,
    k: int = 3,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> list[Citation]:
    """The top ``k`` resolved cases similar to ``query``; empty when nothing matches."""
    pool = [c for c in candidates if c.case_id != query.case_id]
    raw = bm25_scores(query.text, pool)
    best = max(raw.values(), default=0.0)
    out: list[Citation] = []
    for doc in pool:
        parts = {
            "signature_match": signature_match(query.signature, doc.signature),
            "bm25": raw.get(doc.case_id, 0.0) / best if best > 0 else 0.0,
            "structured_match": structured_match(query, doc),
            "graph_similarity": graph_similarity(query.neighbours, doc.neighbours),
            "recency": math.exp(-max(0.0, doc.age_days) / 30.0),
        }
        if parts["signature_match"] == 0.0 and parts["bm25"] == 0.0:
            continue  # ARCH §5.2 honesty: no evidence, no "similar case"
        total = sum(weights.get(name, 0.0) * value for name, value in parts.items())
        out.append(Citation(doc.case_id, round(total, 6), parts))
    out.sort(key=lambda c: (-c.score, c.case_id))
    return out[:k]


__all__ = [
    "DEFAULT_WEIGHTS",
    "Citation",
    "MemoryDoc",
    "bm25",
    "bm25_scores",
    "graph_similarity",
    "rank",
    "signature_match",
    "structured_match",
    "tokens",
]
