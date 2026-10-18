"""Config loader: YAML file -> resolved dict -> ``AppConfig`` (02 §1).

``load_config(path, env)`` is the single module allowed to touch the process
environment (05 §6): the bootstrap entry point passes ``os.environ`` here, and
tests pass a plain mapping, so the loader stays deterministic (04 §3). It is
also the only module that reads YAML (ADR-0004): the ``*_env`` keys are
resolved into ``SecretStr`` values before the pydantic models in
``config.schema`` see them.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from supportagent.config.schema import AppConfig
from supportagent.domain.errors import ConfigError

# 02 §1 secret sources: (section path, `*_env` key in the file, resolved field).
_SECRET_SOURCES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("database",), "url_env", "url"),
    (("servicenow", "auth"), "client_id_env", "client_id"),
    (("servicenow", "auth"), "client_secret_env", "client_secret"),
    (("servicenow", "auth"), "user_env", "user"),
    (("servicenow", "auth"), "password_env", "password"),
    (("identity",), "client_id_env", "client_id"),
    (("identity",), "client_secret_env", "client_secret"),
    (("identity",), "session_secret_env", "session_secret"),
    (("llm",), "api_key_env", "api_key"),
)

# 02 §1: the fixture provider is test/--demo only.
_FIXTURE_ENV_VAR = "SUPPORTAGENT_ENV"
_FIXTURE_ENV_VALUE = "test"


def load_config(path: Path, env: Mapping[str, str]) -> AppConfig:
    """Load and validate ``supportagent.yaml`` (02 §1), resolving ``*_env``.

    Every failure is a ``ConfigError`` naming the offending field or variable.
    """
    resolved = _resolve_secrets(_load_yaml_file(path), env)
    try:
        cfg = AppConfig.model_validate(resolved)
    except ValidationError as exc:
        raise ConfigError(f"config {path}: {exc}") from exc  # noqa: TRY003
    if cfg.llm.provider == "fixture" and env.get(_FIXTURE_ENV_VAR) != _FIXTURE_ENV_VALUE:
        raise ConfigError(  # noqa: TRY003
            "llm.provider 'fixture' is only usable with "
            f"{_FIXTURE_ENV_VAR}={_FIXTURE_ENV_VALUE} (or the --demo CLI flag)"
        )
    return cfg


def _load_yaml_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc  # noqa: TRY003
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc  # noqa: TRY003
    if not isinstance(data, dict):
        raise ConfigError(  # noqa: TRY003
            f"config {path}: expected a YAML mapping at the top level"
        )
    return data


def _resolve_secrets(raw: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    resolved = copy.deepcopy(raw)
    for section, key, field in _SECRET_SOURCES:
        node = _section(resolved, section)
        if node is None or key not in node:
            continue
        variable = node.pop(key)
        if variable is None:
            node[field] = None  # e.g. llm.api_key_env: null for ollama / fixture
        else:
            node[field] = _resolve_variable(variable, key, env)
    return resolved


def _section(resolved: dict[str, Any], section: tuple[str, ...]) -> dict[str, Any] | None:
    node: object = resolved
    for part in section:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def _resolve_variable(variable: object, key: str, env: Mapping[str, str]) -> str:
    if not isinstance(variable, str) or not variable:
        raise ConfigError(  # noqa: TRY003
            f"config: {key} must name a non-empty environment variable"
        )
    value = env.get(variable)
    if value is None:
        raise ConfigError(  # noqa: TRY003
            f"config: missing environment variable '{variable}' required by {key}"
        )
    return value
