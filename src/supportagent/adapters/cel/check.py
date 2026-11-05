"""``CelCheckCompiler`` (ARCH §5, 11-wp WP-110): runbook checks are CEL, never code.

At compile time:

1. ``celpy`` parses the expression; a syntax error is a ``ConfigError``.
2. Every free identifier must be ``result``, ``service`` or ``case``.
   Comprehension variables (``runs.exists(r, …)``) are bound; known CEL
   functions are allowed. Anything else, ``os`` or ``__import__``, is rejected.
3. ``result.<field>`` must be declared by the tool's result schema,
   ``service.<field>`` must be a catalog field, and ``case.<field>`` one of
   the case fields RCA exposes.

At run time the compiled program is evaluated on plain data, with a 1 s
budget. An evaluation error (a field the result doesn't have, a division by
zero) is ``(False, reason)``. A timeout raises ``CheckUnevaluated``: RCA
counts the step as not run, never as a check that didn't hold. CEL has no way
to call Python (``test_cel_never_executes_python``).

The budget is a guard against a pathological expression, not a performance
target. A check takes about a millisecond, but it runs on a worker thread
and needs the interpreter lock, which another thread in the process can
hold for tens of milliseconds. The old 50 ms budget timed out under that
load and reported a held check as false.
"""

from __future__ import annotations

import concurrent.futures
import re
from collections.abc import Mapping

import celpy
from celpy.adapter import json_to_cel

from supportagent.domain.errors import CheckUnevaluated, ConfigError
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.tools import schema_fields

TIMEOUT_S = 1.0
CASE_FIELDS = frozenset({"id", "service", "severity", "classification", "origin", "opened_ts"})
_ROOTS = frozenset({"result", "service", "case"})
_FUNCTIONS = frozenset(
    {
        "size",
        "has",
        "int",
        "uint",
        "double",
        "string",
        "bool",
        "bytes",
        "timestamp",
        "duration",
        "matches",
        "startsWith",
        "endsWith",
        "contains",
        "exists",
        "all",
        "exists_one",
        "map",
        "filter",
        "type",
        "dyn",
    }
)
_KEYWORDS = frozenset({"true", "false", "null", "in"})
_STRING = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_CHAIN = re.compile(r"(?<![\w.])([A-Za-z_]\w*)((?:\.[A-Za-z_]\w*)*)(\s*\()?")
_BOUND = re.compile(
    r"([A-Za-z_][\w.]*)\.(?:exists|all|exists_one|map|filter)\(\s*([A-Za-z_]\w*)\s*,"
)
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="cel")


class CelCheck:
    def __init__(self, expression: str, program: object) -> None:
        self.expression = expression
        self._program = program

    def is_satisfied_by(
        self,
        result: Mapping[str, object],
        service: Mapping[str, object],
        case: Mapping[str, object],
    ) -> tuple[bool, str]:
        activation = {
            "result": json_to_cel(dict(result)),
            "service": json_to_cel(dict(service)),
            "case": json_to_cel(dict(case)),
        }
        future = _POOL.submit(self._program.evaluate, activation)  # type: ignore[attr-defined]
        try:
            value = future.result(timeout=TIMEOUT_S)
        except concurrent.futures.TimeoutError as exc:
            raise CheckUnevaluated(  # noqa: TRY003
                f"check timed out after {int(TIMEOUT_S * 1000)} ms: {self.expression}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — a failing check is "not satisfied"
            return False, f"check could not be evaluated: {type(exc).__name__}"
        if isinstance(value, Exception):
            return False, f"check could not be evaluated: {type(value).__name__}"
        ok = bool(value)
        return ok, f"{self.expression} -> {ok}"


class CelCheckCompiler:
    def __init__(self) -> None:
        self._env = celpy.Environment()

    def compile(self, expr: str, result_schema: Mapping[str, object]) -> CelCheck:
        if not expr.strip() or len(expr) > 1000:  # noqa: PLR2004
            raise ConfigError("check: an expression of 1-1000 characters is required")  # noqa: TRY003
        try:
            ast = self._env.compile(expr)
        except Exception as exc:
            raise ConfigError(f"check {expr!r}: not valid CEL: {exc}") from exc  # noqa: TRY003
        _check_fields(expr, result_schema)
        return CelCheck(expr, self._env.program(ast))


def _check_path(expr: str, path: list[str], fields: set[str]) -> None:
    """Every segment of a ``result`` path must be declared, down to an opaque leaf."""
    current = path[0]
    for segment in path[1:]:
        if not any(f.startswith((current + ".", current + "[].")) for f in fields):
            return  # a leaf (or an undeclared-shape object): nothing more to check
        if f"{current}.{segment}" in fields:
            current = f"{current}.{segment}"
        elif f"{current}[].{segment}" in fields:
            raise ConfigError(  # noqa: TRY003
                f"check {expr!r}: {current} is a list; use a comprehension to read {segment}"
            )
        else:
            name = current.replace("[]", "") + "." + segment
            raise ConfigError(f"check {expr!r}: {name} is not in the tool's result schema")  # noqa: TRY003


def _check_fields(expr: str, result_schema: Mapping[str, object]) -> None:
    code = _STRING.sub("''", expr)
    fields = schema_fields(result_schema)
    # Comprehension variables stand for one element of their receiver list.
    bound: dict[str, str | None] = {}
    for receiver, var in _BOUND.findall(code):
        root, *rest = receiver.split(".")
        base = "result" if root == "result" else bound.get(root)
        bound[var] = ".".join([base, *rest]) + "[]" if base else None
    for match in _CHAIN.finditer(code):
        root, rest, call = match.group(1), match.group(2), match.group(3)
        if root in _KEYWORDS or root.isdigit():
            continue
        if call and not rest and root in _FUNCTIONS:
            continue
        segments = [p for p in rest.split(".") if p]
        if call and segments and segments[-1] in _FUNCTIONS:
            segments = segments[:-1]  # a method call: ``x.exists(``, ``s.startsWith(``
        if root in bound:
            base = bound[root]
            if base is not None and segments:
                _check_path(expr, [base, *segments], fields)
            continue
        if root not in _ROOTS:
            raise ConfigError(f"check {expr!r}: unknown identifier {root!r}")  # noqa: TRY003
        if not segments:
            continue
        if root == "result":
            _check_path(expr, ["result", *segments], fields)
        elif root == "service" and segments[0] not in ServiceEntry.model_fields:
            raise ConfigError(f"check {expr!r}: service.{segments[0]} is not a catalog field")  # noqa: TRY003
        elif root == "case" and segments[0] not in CASE_FIELDS:
            raise ConfigError(f"check {expr!r}: case.{segments[0]} is not exposed to checks")  # noqa: TRY003


__all__ = ["CASE_FIELDS", "CelCheck", "CelCheckCompiler"]
