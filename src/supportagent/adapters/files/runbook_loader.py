"""Runbooks and probes (ARCH §5.1, 11-wp WP-110): every check compiled at load.

``load_runbooks`` reads ``runbooks/*.yaml`` (except ``probes.yaml``) and
rejects a runbook unless all of these hold:

- every step's hypothesis is in the hypothesis taxonomy;
- every step's tool is registered;
- every ``$service.<field>`` argument names a catalog field;
- every ``check`` compiles (CEL) against that tool's declared result schema;
- ``comms_if_supported.audience`` is ``[internal]`` (the model enforces it).

``load_probes`` reads ``runbooks/probes.yaml``: one registered probe per
hypothesis, used when no runbook applies, validated the same way. A probe
naming an unregistered tool is *dropped*, so its hypothesis can never be
SUPPORTED (ARCH §5.5).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from pathlib import Path

from pydantic import ValidationError

from supportagent.adapters.files._yaml import load_yaml_file
from supportagent.domain.errors import ConfigError
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.model.runbook import Probe, Runbook
from supportagent.ports.checks import CheckCompiler, CompiledCheck

_REF = re.compile(r"^\$service\.([a-z][a-z0-9_]*)(?:\[\d+\])?$")


def _check_args(where: str, args: Mapping[str, str | int]) -> None:
    for key, value in args.items():
        if isinstance(value, str) and value.startswith("$"):
            match = _REF.match(value)
            if match is None or match.group(1) not in ServiceEntry.model_fields:
                raise ConfigError(f"{where}: argument {key}={value!r} is not a catalog field")  # noqa: TRY003


def load_runbooks(
    directory: Path,
    *,
    hypotheses: Collection[str],
    tool_schemas: Mapping[str, Mapping[str, object]],
    compiler: CheckCompiler,
) -> list[tuple[Runbook, dict[str, CompiledCheck]]]:
    out: list[tuple[Runbook, dict[str, CompiledCheck]]] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "probes.yaml":
            continue
        try:
            runbook = Runbook.model_validate(load_yaml_file(path))
        except ValidationError as exc:
            raise ConfigError(f"{path}: {exc}") from exc  # noqa: TRY003
        checks: dict[str, CompiledCheck] = {}
        for step in runbook.steps:
            where = f"{path}: {runbook.id} step {step.id}"
            if step.hypothesis not in hypotheses:
                raise ConfigError(f"{where}: unknown hypothesis {step.hypothesis!r}")  # noqa: TRY003
            schema = tool_schemas.get(step.tool)
            if schema is None:
                raise ConfigError(f"{where}: tool {step.tool!r} is not registered")  # noqa: TRY003
            _check_args(where, step.args)
            checks[step.id] = compiler.compile(step.check, schema)
        out.append((runbook, checks))
    return out


def load_probes(
    path: Path,
    *,
    hypotheses: Collection[str],
    tool_schemas: Mapping[str, Mapping[str, object]],
    compiler: CheckCompiler,
) -> dict[str, tuple[Probe, CompiledCheck]]:
    if not path.is_file():
        return {}
    raw = load_yaml_file(path).get("probes") or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'probes' must be a mapping")  # noqa: TRY003
    out: dict[str, tuple[Probe, CompiledCheck]] = {}
    for hypothesis, body in raw.items():
        where = f"{path}: probe {hypothesis}"
        if hypothesis not in hypotheses:
            raise ConfigError(f"{where}: unknown hypothesis")  # noqa: TRY003
        try:
            probe = Probe.model_validate(body)
        except ValidationError as exc:
            raise ConfigError(f"{where}: {exc}") from exc  # noqa: TRY003
        schema = tool_schemas.get(probe.tool)
        if schema is None:
            continue  # ARCH §5.5: no registered tool, no probe, never SUPPORTED
        _check_args(where, probe.args)
        out[str(hypothesis)] = (probe, compiler.compile(probe.check, schema))
    return out


__all__ = ["load_probes", "load_runbooks"]
