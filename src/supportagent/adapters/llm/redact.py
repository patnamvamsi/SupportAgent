"""Redaction (01 §7.13, INV-06): replace secrets with per-class placeholders.

The :class:`Redactor` runs every detector over a text, picks a maximal
non-overlapping set of spans (longest-first on overlap), and rewrites each
span to a 1-based per-class placeholder (``<EMAIL_1>``, ``<AWS_ACCESS_KEY_2>``).
The placeholder *map* (secret -> placeholder) is kept in memory only and never
returned — INV-06 requires that no secret value survives into a provider
request or an event payload; only per-class *counts* do.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from supportagent.domain.detectors import DetectorClass
from supportagent.ports.secrets import SecretDetector, SecretSpan


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """The redacted text plus per-class counts (never the secret values)."""

    text: str
    counts_by_class: dict[str, int] = field(default_factory=dict)


class Redactor:
    """Replace detected secrets with per-class placeholders (INV-06)."""

    def __init__(self, detectors: Sequence[SecretDetector]) -> None:
        self._detectors = list(detectors)

    def redact(self, text: str) -> RedactionResult:
        """Redact ``text``; returns the rewritten text and per-class counts."""
        spans: list[SecretSpan] = []
        for detector in self._detectors:
            spans.extend(detector.find(text))

        # Left-to-right; on a shared start the longer span wins (stable sort).
        spans.sort(key=lambda s: (s.start, -(s.end - s.start)))

        picked: list[SecretSpan] = []
        last_end = -1
        for span in spans:
            if span.start >= last_end:
                picked.append(span)
                last_end = span.end

        counters: dict[DetectorClass, int] = {}
        replacements: list[tuple[int, int, str]] = []
        for span in picked:
            cls = span.detector_class
            counters[cls] = counters.get(cls, 0) + 1
            n = counters[cls]
            replacements.append((span.start, span.end, f"<{cls.value.upper()}_{n}>"))

        result = text
        for start, end, placeholder in sorted(replacements, key=lambda r: -r[0]):
            result = result[:start] + placeholder + result[end:]

        counts_by_class = {cls.value: count for cls, count in counters.items()}
        return RedactionResult(text=result, counts_by_class=counts_by_class)
