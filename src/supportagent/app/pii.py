"""PII guard (ADR-0021 §4a): a stage result, as the model may see it.

``PiiGuard.clean`` keeps the stage's allowed fields, then analyses every
string left and pseudonymises it with one question's :class:`Pseudonyms`, so
the same value gets the same placeholder across stages. Mapping keys are
left as they are: they come from the tool's declared schema, not the data.

It fails closed: ``PiiUnavailable`` from the analyser propagates, and the
caller must not send the stage result anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from supportagent.domain.fieldpath import project
from supportagent.domain.pii import Pseudonyms
from supportagent.ports.pii import PiiAnalyzer


class PiiGuard:
    """One question's guard: allow-list, then pseudonymise (in memory only)."""

    def __init__(self, analyzer: PiiAnalyzer, *, keep: Iterable[str] = ()) -> None:
        self._analyzer = analyzer
        self.pseudonyms = Pseudonyms(keep)

    def clean(self, data: Mapping[str, object], fields: Iterable[str]) -> dict[str, object]:
        """``data`` reduced to ``fields``, with every PII value replaced."""
        return {key: self._walk(value) for key, value in project(data, fields).items()}

    def text(self, text: str) -> str:
        """One free text (a log line) pseudonymised."""
        return self.pseudonyms.apply(text, self._analyzer.analyze(text))

    def _walk(self, value: object) -> object:
        if isinstance(value, str):
            return self.text(value) if value else value
        if isinstance(value, Mapping):
            return {key: self._walk(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self._walk(item) for item in value]
        return value


__all__ = ["PiiGuard"]
