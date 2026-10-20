"""Eval file loading (04 §8, WP-016): case files and the manifest, as plain mappings.

ADR-0004 keeps YAML parsing at the file boundary; ``supportagent.evals``
validates the documents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from supportagent.adapters.files._yaml import load_yaml_file


def load_eval_documents(directory: Path) -> list[tuple[str, dict[str, Any]]]:
    """``(file stem, document)`` for every ``*.yaml`` in ``directory``, by file name."""
    return [(path.stem, load_yaml_file(path)) for path in sorted(directory.glob("*.yaml"))]


def load_yaml_document(path: Path) -> dict[str, Any]:
    """One YAML mapping (the eval manifest, or a config file read without secrets)."""
    return load_yaml_file(path)


__all__ = ["load_eval_documents", "load_yaml_document"]
