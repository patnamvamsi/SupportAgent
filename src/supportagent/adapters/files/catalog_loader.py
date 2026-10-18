"""File adapter for the service catalog (01 §4, ARCH §7)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from supportagent.adapters.files._yaml import load_yaml_file
from supportagent.domain.errors import ConfigError
from supportagent.domain.model import Catalog


def load_catalog(path: Path) -> Catalog:
    """Load ``catalog/services.yaml``; ``ConfigError`` naming the field on failure."""
    try:
        return Catalog.model_validate(load_yaml_file(path))
    except ValidationError as exc:
        raise ConfigError(f"catalog {path}: {exc}") from exc  # noqa: TRY003
