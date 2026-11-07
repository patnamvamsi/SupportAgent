"""PII-detection port (ADR-0021 §4a).

A ``PiiAnalyzer`` finds personal data in a text. The pseudonymiser
(``domain.pii``) turns its spans into placeholders. It raises
``PiiUnavailable`` when it can't analyse (not installed, model missing,
an engine error), and callers fail closed: nothing unanalysed reaches the
model.
"""

from __future__ import annotations

from typing import Protocol

from supportagent.domain.pii import PiiSpan


class PiiAnalyzer(Protocol):
    def analyze(self, text: str) -> list[PiiSpan]:
        """Every PII span in ``text`` (may be empty); raises ``PiiUnavailable``."""
        ...


__all__ = ["PiiAnalyzer"]
