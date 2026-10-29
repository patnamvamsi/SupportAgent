"""Eval file loading (04 §8, WP-016): case files and the manifest, as plain mappings.

ADR-0004 keeps YAML parsing at the file boundary; ``supportagent.evals``
validates the documents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from supportagent.adapters.files._yaml import front_matter, load_yaml_any, load_yaml_file
from supportagent.domain.errors import ConfigError


def load_eval_documents(directory: Path) -> list[tuple[str, dict[str, Any]]]:
    """``(file stem, document)`` for every ``*.yaml`` in ``directory``, by file name."""
    return [(path.stem, load_yaml_file(path)) for path in sorted(directory.glob("*.yaml"))]


def load_yaml_document(path: Path) -> dict[str, Any]:
    """One YAML mapping (the eval manifest, or a config file read without secrets)."""
    return load_yaml_file(path)


def load_yaml_records(path: Path) -> list[dict[str, Any]]:
    """A YAML list of mappings (ServiceNow records in the onboarding lab)."""
    data = load_yaml_any(path)
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ConfigError(f"{path}: expected a YAML list of mappings")  # noqa: TRY003
    return data


def load_front_matter(path: Path) -> dict[str, Any]:
    """A Markdown page's front matter (Confluence pages in the onboarding lab)."""
    return front_matter(path)


__all__ = ["load_eval_documents", "load_front_matter", "load_yaml_document", "load_yaml_records"]
