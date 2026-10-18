"""File adapters for the classification and hypothesis taxonomies (ARCH §12)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from supportagent.adapters.files._yaml import load_yaml_file
from supportagent.domain.errors import ConfigError
from supportagent.domain.model import ClassificationTaxonomy, HypothesisTaxonomy


def load_classification(path: Path) -> ClassificationTaxonomy:
    """Load ``taxonomy/classification.yaml`` (semver version, 06 §3)."""
    try:
        return ClassificationTaxonomy.model_validate(load_yaml_file(path))
    except ValidationError as exc:
        raise ConfigError(f"classification {path}: {exc}") from exc  # noqa: TRY003


def load_hypotheses(path: Path) -> HypothesisTaxonomy:
    """Load ``taxonomy/hypotheses.yaml`` (ARCH §12.1)."""
    try:
        return HypothesisTaxonomy.model_validate(load_yaml_file(path))
    except ValidationError as exc:
        raise ConfigError(f"hypotheses {path}: {exc}") from exc  # noqa: TRY003
