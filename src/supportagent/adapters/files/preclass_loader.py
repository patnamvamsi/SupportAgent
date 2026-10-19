"""File adapter for the pre-classifier rules (ARCH §4.2, 10-wp WP-010)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from supportagent.adapters.files._yaml import load_yaml_file
from supportagent.domain.errors import ConfigError
from supportagent.domain.model import RuleSet


def load_preclass_rules(path: Path) -> RuleSet:
    """Load ``taxonomy/preclass_rules.yaml``; bad fields or regexes fail here."""
    try:
        return RuleSet.model_validate(load_yaml_file(path))
    except ValidationError as exc:
        raise ConfigError(f"preclass rules {path}: {exc}") from exc  # noqa: TRY003
