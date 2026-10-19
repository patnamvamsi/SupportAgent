"""Secret-detection port (01 §7.13, INV-06).

A ``SecretDetector`` finds spans of one secret class in a text; the redactor
(adapters) turns those spans into per-class placeholders and counts. The port
is intentionally small so the domain can name the contract without pydantic
or any concrete detector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from supportagent.domain.detectors import DetectorClass


@dataclass(frozen=True, slots=True)
class SecretSpan:
    """A span of secret text, tagged with the class that found it.

    ``0 <= start < end <= len(text)`` (half-open, like a slice).
    """

    start: int
    end: int
    detector_class: DetectorClass


class SecretDetector(Protocol):
    """Finds spans of one secret class (01 §7.13)."""

    detector_class: DetectorClass

    def find(self, text: str) -> list[SecretSpan]:
        """All spans of this class in ``text`` (may be empty)."""
        ...
