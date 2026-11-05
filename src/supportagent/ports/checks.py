"""Check compilation (01 §3, ARCH §5; 11-wp WP-110): the Specification pattern.

A runbook step's ``check`` is a CEL expression compiled against the tool's
declared result schema and the ``service`` / ``case`` fields. Compiling
fails on unknown fields. The RCA service only ever evaluates compiled
checks; it never sees the expression language.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class CompiledCheck(Protocol):
    expression: str

    def is_satisfied_by(
        self,
        result: Mapping[str, object],
        service: Mapping[str, object],
        case: Mapping[str, object],
    ) -> tuple[bool, str]:
        """``(ok, why)``; an evaluation error is ``(False, reason)``.

        Raises ``CheckUnevaluated`` when the check couldn't be evaluated at all
        (its budget ran out): the caller must not read that as ``False``.
        """
        ...


class CheckCompiler(Protocol):
    def compile(self, expr: str, result_schema: Mapping[str, object]) -> CompiledCheck:
        """Raises ``ConfigError`` on invalid CEL or an unknown field."""
        ...


__all__ = ["CheckCompiler", "CompiledCheck"]
