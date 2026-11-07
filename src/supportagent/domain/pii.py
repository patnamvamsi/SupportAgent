"""PII pseudonymisation (ADR-0021 §4a): what the model may see of a stage result.

Two steps, in order:

- ``fieldpath.project`` keeps only the fields a flow stage allows. Fields
  that aren't listed never reach detection or the model.
- :class:`Pseudonyms` replaces each detected PII value with a placeholder that
  is stable for one question (``<PERSON_1>`` every time the same name appears),
  so the model can still tell two stages saw the same customer. The map lives
  in memory only and is never stored, logged or sent (INV-06): callers read
  :attr:`Pseudonyms.counts`, never the values.

Values the question must keep (the traced object's own id) pass through: a
span that lies inside an occurrence of a kept value isn't replaced. Pure: the
spans come from a ``PiiAnalyzer`` port.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_ENTITY = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class PiiSpan:
    """One detected value: ``text[start:end]`` is ``entity`` with ``score`` (0..1)."""

    start: int
    end: int
    entity: str
    score: float


def valid_entity(name: str) -> bool:
    """An entity name is UPPER_SNAKE, as Presidio names them (``PERSON``, ``IBAN_CODE``)."""
    return bool(_ENTITY.match(name))


class Pseudonyms:
    """One question's value -> placeholder map (in memory only, ADR-0021 §4a)."""

    def __init__(self, keep: Iterable[str] = ()) -> None:
        self._keep = tuple(sorted({v for v in keep if v}, key=len, reverse=True))
        self._names: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = {}

    @property
    def counts(self) -> dict[str, int]:
        """Distinct values pseudonymised per entity: safe to record, never the values."""
        return dict(sorted(self._counts.items()))

    def placeholder(self, entity: str, value: str) -> str:
        key = (entity, value)
        name = self._names.get(key)
        if name is None:
            n = self._counts.get(entity, 0) + 1
            self._counts[entity] = n
            name = f"<{entity}_{n}>"
            self._names[key] = name
        return name

    def apply(self, text: str, spans: Iterable[PiiSpan]) -> str:
        """``text`` with every span outside a kept value replaced by its placeholder."""
        kept = self._kept_ranges(text)
        picked: list[PiiSpan] = []
        # Highest score first, then the longer span; a span overlapping a picked one loses.
        for span in sorted(spans, key=lambda s: (-s.score, -(s.end - s.start), s.start)):
            if not 0 <= span.start < span.end <= len(text):
                continue
            if any(a <= span.start and span.end <= b for a, b in kept):
                continue
            if any(span.start < p.end and p.start < span.end for p in picked):
                continue
            picked.append(span)
        out = text
        for span in sorted(picked, key=lambda s: -s.start):
            name = self.placeholder(span.entity, text[span.start : span.end])
            out = out[: span.start] + name + out[span.end :]
        return out

    def _kept_ranges(self, text: str) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        for value in self._keep:
            start = text.find(value)
            while start != -1:
                ranges.append((start, start + len(value)))
                start = text.find(value, start + 1)
        return ranges


__all__ = ["PiiSpan", "Pseudonyms", "valid_entity"]
