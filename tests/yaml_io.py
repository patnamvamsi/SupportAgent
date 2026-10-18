"""YAML file I/O for the WP-002 loader tests.

Standalone module with a top-level ``yaml`` import: the WP-002 red commit must
still collect and run the WP-001 suite without pyyaml installed — pyyaml lands
with the green phase under ADR-0003, so no shared helper may import it at top
level.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def write_yaml_file(tmp_path: Path, name: str, data: dict[str, Any]) -> Path:
    """Serialize a builder dict to a YAML file for the loader tests (02 §1)."""
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path
