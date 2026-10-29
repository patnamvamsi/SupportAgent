"""Shared YAML file loading (ADR-0004): the adapter boundary for raw YAML.

Only ``config/loader.py`` (the env-resolution step) and this helper import
``yaml``; domain/ and ports/ are kept parser-agnostic by import-linter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from supportagent.domain.errors import ConfigError


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Load a YAML file with a top-level mapping; ``ConfigError`` otherwise."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc  # noqa: TRY003
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc  # noqa: TRY003
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a YAML mapping at the top level")  # noqa: TRY003
    return data


def load_yaml_any(path: Path) -> object:
    """Load a YAML file whatever its top level (a list of records, say)."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc  # noqa: TRY003
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc  # noqa: TRY003


def front_matter(path: Path) -> dict[str, Any]:
    """A Markdown file's ``---`` front matter as a mapping; empty when it has none."""
    parts = path.read_text(encoding="utf-8").split("---", 2)
    if len(parts) != 3 or parts[0].strip():  # noqa: PLR2004 — before, inside, after
        return {}
    try:
        data: object = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid front matter in {path}: {exc}") from exc  # noqa: TRY003
    return data if isinstance(data, dict) else {}
