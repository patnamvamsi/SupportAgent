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
import os
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

# 03 §3: the AES-256-GCM master key that wraps the per-tenant data keys.
# Like the other secret sources, it lives in an environment variable — but it
# is a fixed name (03 §3), not a `*_env` key in the config file.
_MASTER_KEY_ENV_VAR = "SA_MASTER_KEY_ENV"
_MASTER_KEY_LEN = 32


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


def load_config_default(path: Path = Path("supportagent.yaml")) -> AppConfig:
    """Entry-point convenience: load ``path`` with the process environment.

    05 §6: this module is the single one allowed to touch the environment;
    entrypoints (01 §2: thin, no direct I/O of secrets) resolve the config
    through here instead of reading ``os.environ`` themselves.
    """
    return load_config(path, os.environ)


def load_master_key(env: Mapping[str, str]) -> bytes:
    """Resolve the AES-256-GCM master key from ``SA_MASTER_KEY_ENV`` (03 §3).

    The value is a hex-encoded 32-byte key (64 hex characters). Every failure
    is a ``ConfigError`` naming the variable — startup fails, per 01 §6.
    """
    raw = env.get(_MASTER_KEY_ENV_VAR)
    if raw is None or not raw.strip():
        raise ConfigError(  # noqa: TRY003
            f"missing master key: set {_MASTER_KEY_ENV_VAR} to a 64-character hex string (03 §3)"
        )
    try:
        key = bytes.fromhex(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{_MASTER_KEY_ENV_VAR} must be hex-encoded: {exc}") from exc  # noqa: TRY003
    if len(key) != _MASTER_KEY_LEN:
        raise ConfigError(  # noqa: TRY003
            f"{_MASTER_KEY_ENV_VAR} must encode exactly {_MASTER_KEY_LEN} bytes "
            f"(AES-256-GCM), got {len(key)}"
        )
    return key


def load_master_key_default() -> bytes:
    """Entry-point convenience: read the master key from the process env (05 §6)."""
    return load_master_key(os.environ)


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
