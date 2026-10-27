"""Tool-call rules (ARCH §7, §5.1; 11-wp WP-107): bound arguments, declared results.

**Arguments are bound from the service catalog, not from free strings**
(``test_tool_args_bound_from_service_only``). A call template (from a runbook
or the RCA planner) maps each argument to one of:

- ``"$service.<field>"`` or ``"$service.<field>[i]"``: a catalog field of the
  case's resolved service (``repos``, ``log_paths``, ``dags``, ``cmdb_ci``, ...);
- an integer (a window, a limit);
- a string literal, **only** for keys the tool declares as ``literal_args``
  (a log grep pattern from a reviewed runbook).

Anything else is a ``DomainValidationError``. Ticket text can never become a
path, repo or DAG id.

**Results** are checked against the tool's declared JSON Schema subset:
``type``, ``properties``, ``required``, ``items``, ``enum``, ``maxItems``.
That's enough for the CEL field check (WP-110) and stays dependency-free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.model.catalog import ServiceEntry

_REF = re.compile(r"^\$service\.([a-z][a-z0-9_]*)(?:\[(\d+)\])?$")
_TYPES: Mapping[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def bind_args(
    template: Mapping[str, object], service: ServiceEntry, *, literal_args: frozenset[str]
) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in template.items():
        if isinstance(value, bool) or (not isinstance(value, int | str)):
            raise DomainValidationError(f"argument {key!r}: unsupported value {value!r}")  # noqa: TRY003
        if isinstance(value, int):
            out[key] = value
            continue
        match = _REF.match(value)
        if match is None:
            if key in literal_args:
                out[key] = value
                continue
            raise DomainValidationError(  # noqa: TRY003
                f"argument {key!r} must be bound from $service.<field>, not a free string"
            )
        field_name, index = match.group(1), match.group(2)
        if field_name not in ServiceEntry.model_fields:
            raise DomainValidationError(f"argument {key!r}: no catalog field {field_name!r}")  # noqa: TRY003
        bound = getattr(service, field_name)
        if index is not None:
            if not isinstance(bound, list) or int(index) >= len(bound):
                raise DomainValidationError(  # noqa: TRY003
                    f"argument {key!r}: service {service.name} has no {field_name}[{index}]"
                )
            bound = bound[int(index)]
        if bound in (None, [], ""):
            raise DomainValidationError(  # noqa: TRY003
                f"argument {key!r}: service {service.name} has no {field_name}"
            )
        out[key] = bound
    return out


def schema_errors(  # noqa: PLR0912 — one branch per supported keyword
    value: object, schema: Mapping[str, object], path: str = "$"
) -> list[str]:
    """Every place ``value`` breaks ``schema`` (the supported subset); empty when valid."""
    errors: list[str] = []
    kind = schema.get("type")
    if kind is not None:
        kinds = [str(k) for k in kind] if isinstance(kind, list) else [str(kind)]
        allowed = tuple(t for k in kinds for t in _TYPES[k])
        is_bool = isinstance(value, bool)
        if not isinstance(value, allowed) or (is_bool and "boolean" not in kinds):
            return [f"{path}: expected {kind}, got {type(value).__name__}"]
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path}: {value!r} not in {enum}")
    if isinstance(value, dict):
        required = schema.get("required", [])
        for name in required if isinstance(required, list) else []:
            if name not in value:
                errors.append(f"{path}.{name}: required")
        props = schema.get("properties", {})
        if isinstance(props, Mapping):
            for name, sub in props.items():
                if name in value and isinstance(sub, Mapping):
                    errors.extend(schema_errors(value[name], sub, f"{path}.{name}"))
    if isinstance(value, list | tuple):
        limit = schema.get("maxItems")
        if isinstance(limit, int) and len(value) > limit:
            errors.append(f"{path}: {len(value)} items > maxItems {limit}")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for i, item in enumerate(value):
                errors.extend(schema_errors(item, items, f"{path}[{i}]"))
    return errors


def schema_fields(schema: Mapping[str, object], prefix: str = "result") -> set[str]:
    """Dotted field paths a schema declares (for compile-time CEL field checks, WP-110)."""
    out: set[str] = set()
    props = schema.get("properties", {})
    if not isinstance(props, Mapping):
        return out
    for name, sub in props.items():
        if not isinstance(sub, Mapping):
            continue
        dotted = f"{prefix}.{name}"
        out.add(dotted)
        out |= schema_fields(sub, dotted)
        items = sub.get("items")
        if isinstance(items, Mapping):
            out |= schema_fields(items, f"{dotted}[]")
    return out


__all__ = ["bind_args", "schema_errors", "schema_fields"]
