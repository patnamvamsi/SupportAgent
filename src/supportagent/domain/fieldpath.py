"""Field paths into a tool result (ADR-0021): ``rows[].status``, ``matches[].ts``.

A path is dotted; ``[]`` steps into every item of a list. Flows use paths to
say which fields the model may see (:func:`project`) and where a stage's
evidence is (:func:`values_at`). Pure.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[\])?(\.[A-Za-z_][A-Za-z0-9_]*(\[\])?)*$")


def valid_path(path: str) -> bool:
    return bool(_PATH.match(path))


def values_at(data: object, path: str) -> list[object]:
    """Every value at ``path``, in order; empty when nothing is there.

    A path ending in ``[]`` yields the list's items; one ending in a plain key
    yields the value itself (a list stays one value).
    """
    current: list[object] = [data]
    for step in path.split("."):
        many = step.endswith("[]")
        key = step.removesuffix("[]")
        nxt: list[object] = []
        for value in current:
            if not isinstance(value, Mapping) or key not in value:
                continue
            inner = value[key]
            if many:
                if isinstance(inner, list | tuple):
                    nxt.extend(inner)
            else:
                nxt.append(inner)
        current = nxt
    return current


def project(data: Mapping[str, object], paths: Iterable[str]) -> dict[str, object]:
    """Only the allowed fields of ``data``; a missing field is simply absent."""
    out: dict[str, object] = {}
    for path in paths:
        _merge(out, _pick(data, path.split(".")))
    return out


class _Missing:
    pass


_MISSING = _Missing()


def _pick(value: object, steps: Sequence[str]) -> object:
    if not steps:
        return value
    step, rest = steps[0], steps[1:]
    many = step.endswith("[]")
    key = step.removesuffix("[]")
    if not isinstance(value, Mapping) or key not in value:
        return _MISSING
    inner = value[key]
    if many:
        if not isinstance(inner, list | tuple):
            return _MISSING
        items = [_pick(item, rest) for item in inner]
        return {key: [item for item in items if item is not _MISSING]}
    picked = _pick(inner, rest)
    return _MISSING if picked is _MISSING else {key: picked}


def _merge(into: dict[str, object], picked: object) -> None:
    if not isinstance(picked, dict):
        return
    for key, value in picked.items():
        have = into.get(key)
        if isinstance(have, dict) and isinstance(value, dict):
            _merge(have, value)
        elif isinstance(have, list) and isinstance(value, list) and len(have) == len(value):
            into[key] = [_merged(a, b) for a, b in zip(have, value, strict=True)]
        else:
            into[key] = value


def _merged(a: object, b: object) -> object:
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        _merge(out, b)
        return out
    return b


__all__ = ["project", "valid_path", "values_at"]
