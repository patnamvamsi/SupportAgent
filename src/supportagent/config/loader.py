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
from supportagent.config.system_manifest import SystemManifest
from supportagent.domain.errors import ConfigError
from supportagent.domain.faults import Fault, parse_faults

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
    (("llm", "onboarding"), "api_key_env", "api_key"),
    # 11-wp WP-107: tool credentials (each section optional).
    (("tools", "airflow"), "token_env", "token"),
    (("tools", "github"), "token_env", "token"),
    (("tools", "oncall"), "pagerduty_token_env", "pagerduty_token"),
    (("tools", "oncall"), "opsgenie_token_env", "opsgenie_token"),
    # WP-M04: the pack tools' credentials.
    (("tools", "prometheus"), "token_env", "token"),
    (("tools", "loki"), "token_env", "token"),
    (("tools", "kubernetes"), "token_env", "token"),
    (("paging", "pagerduty"), "token_env", "token"),
    (("paging", "opsgenie"), "api_key_env", "api_key"),
)

# 02 §1: the fixture provider is test/--demo only.
_FIXTURE_ENV_VAR = "SUPPORTAGENT_ENV"
_FAULT_ENV_VAR = "SUPPORTAGENT_FAULT_INJECT"
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


def load_config_offline(path: Path) -> AppConfig:
    """``path`` for an **offline** fixture eval run (10-wp WP-016): no secrets needed.

    Every ``*_env`` secret becomes an unused placeholder, and the LLM becomes
    the fixture provider (the ``test`` context 02 §1 allows it in). The result
    can't reach ServiceNow, an IdP or an LLM, and is only for ``eval run``
    without ``--live``.
    """
    raw = _load_yaml_file(path)
    placeholder = "offline-eval-placeholder-not-a-secret"
    env = {
        str(node[key]): placeholder
        for section, key, _ in _SECRET_SOURCES
        if (node := _section(raw, section)) is not None and isinstance(node.get(key), str)
    }
    for hook in raw.get("webhooks") or []:
        if isinstance(hook, dict) and isinstance(hook.get("secret_env"), str):
            env[hook["secret_env"]] = placeholder
    for db in _postgres_databases(raw).values():
        if isinstance(db, dict) and isinstance(db.get("dsn_env"), str):
            env[db["dsn_env"]] = placeholder
    for server in _mcp_servers(raw).values():
        if isinstance(server, dict) and isinstance(server.get("token_env"), str):
            env[server["token_env"]] = placeholder
    for source in _knowledge_sources(raw):
        for key in ("user_env", "token_env"):
            if isinstance(source.get(key), str):
                env[source[key]] = placeholder
    resolved = _resolve_secrets(raw, env)
    llm = resolved.get("llm")
    if isinstance(llm, dict):
        llm["provider"] = "fixture"
        llm["api_key"] = None
        llm["base_url"] = None
    try:
        return AppConfig.model_validate(resolved)
    except ValidationError as exc:
        raise ConfigError(f"config {path}: {exc}") from exc  # noqa: TRY003


def load_fault_injection(env: Mapping[str, str]) -> frozenset[Fault]:
    """``SUPPORTAGENT_FAULT_INJECT`` (11-wp WP-106): the faults to inject, or none."""
    return parse_faults(env.get(_FAULT_ENV_VAR))


def load_fault_injection_default() -> frozenset[Fault]:
    return load_fault_injection(os.environ)


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
    # 11-wp WP-113: each webhook source carries its own ``secret_env``.
    for hook in resolved.get("webhooks") or []:
        if isinstance(hook, dict) and "secret_env" in hook:
            hook["secret"] = _resolve_variable(hook.pop("secret_env"), "secret_env", env)
    # WP-M04: each read-only Postgres connection carries its own ``dsn_env``.
    for db in _postgres_databases(resolved).values():
        if isinstance(db, dict) and "dsn_env" in db:
            db["dsn"] = _resolve_variable(db.pop("dsn_env"), "dsn_env", env)
    # WP-M06: each MCP server carries its own ``token_env``.
    for server in _mcp_servers(resolved).values():
        if isinstance(server, dict) and "token_env" in server:
            server["token"] = _resolve_variable(server.pop("token_env"), "token_env", env)
    _resolve_knowledge_secrets(resolved, env)
    return resolved


def load_system_manifest(
    path: Path, env: Mapping[str, str], *, need_secrets: bool = True
) -> SystemManifest:
    """``system.yaml`` (ADR-0015 §1), resolving each source's ``*_env`` credentials.

    ``need_secrets=False`` (the onboarding lab, ``onboard detect``) leaves an
    unset variable unresolved instead of failing: nothing is read remotely.
    """
    raw = _load_yaml_file(path)
    sources = raw.get("sources")
    found = sources if isinstance(sources, dict) else {}
    remote = [*(found.get("confluence") or []), *(found.get("git") or [])]
    for source in remote:
        if not isinstance(source, dict):
            continue
        for key, target in (("user_env", "user"), ("token_env", "token")):
            if key not in source:
                continue
            variable = source.pop(key)
            if need_secrets:
                source[target] = _resolve_variable(variable, key, env)
            elif isinstance(variable, str) and env.get(variable):
                source[target] = env[variable]
    try:
        return SystemManifest.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"system manifest {path}: {exc}") from exc  # noqa: TRY003


def load_system_manifest_default(path: Path, *, need_secrets: bool = True) -> SystemManifest:
    """Entry-point convenience: ``path`` with the process environment (05 §6)."""
    return load_system_manifest(path, os.environ, need_secrets=need_secrets)


def _resolve_knowledge_secrets(resolved: dict[str, Any], env: Mapping[str, str]) -> None:
    """BL-051: each remote knowledge source carries its own ``user_env`` / ``token_env``."""
    for source in _knowledge_sources(resolved):
        for key, target in (("user_env", "user"), ("token_env", "token")):
            if key in source:
                source[target] = _resolve_variable(source.pop(key), key, env)


def _knowledge_sources(raw: dict[str, Any]) -> list[dict[str, Any]]:
    node = _section(raw, ("knowledge",))
    sources = node.get("sources") if node is not None else None
    return [s for s in sources if isinstance(s, dict)] if isinstance(sources, list) else []


def _mcp_servers(raw: dict[str, Any]) -> dict[str, Any]:
    node = _section(raw, ("tools", "mcp_servers"))
    return node if node is not None else {}


def _postgres_databases(raw: dict[str, Any]) -> dict[str, Any]:
    node = _section(raw, ("tools", "postgres"))
    databases = node.get("databases") if node is not None else None
    return databases if isinstance(databases, dict) else {}


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
