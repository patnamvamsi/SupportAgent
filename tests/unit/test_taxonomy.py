"""WP-002: taxonomy + hypotheses models and file loaders (ARCH §12).

Canonical test IDs from 10-wp §WP-002:
``test_taxonomy_label_pattern``, ``test_hypotheses_excludes_reference_known_ids``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supportagent.adapters.files.taxonomy_loader import load_classification, load_hypotheses
from supportagent.domain.errors import ConfigError
from tests.builders import a_classification_file, a_hypotheses_file
from tests.yaml_io import write_yaml_file


def test_taxonomy_label_pattern(tmp_path: Path) -> None:
    # 01 §4 / ARCH §12: labels match ^[a-z0-9_]+/[a-z0-9_]+$ — case and shape
    # are validated at load time.
    data = a_classification_file()
    data["labels"][0]["id"] = "Database/Saturation"
    path = write_yaml_file(tmp_path, "classification.yaml", data)
    with pytest.raises(ConfigError, match="labels"):
        load_classification(path)


def test_taxonomy_rejects_bad_semver_version(tmp_path: Path) -> None:
    # 06 §3: taxonomy version is MAJOR.MINOR.PATCH.
    data = a_classification_file()
    data["version"] = "1.0"
    path = write_yaml_file(tmp_path, "classification.yaml", data)
    with pytest.raises(ConfigError, match="version"):
        load_classification(path)


def test_hypotheses_excludes_reference_known_ids(tmp_path: Path) -> None:
    # ARCH §12.1: excludes is a declarative conflict set over known ids.
    data = a_hypotheses_file()
    data["hypotheses"][1]["excludes"] = ["not_a_hypothesis"]
    path = write_yaml_file(tmp_path, "hypotheses.yaml", data)
    with pytest.raises(ConfigError, match="not_a_hypothesis"):
        load_hypotheses(path)


def test_hypotheses_rejects_bad_id(tmp_path: Path) -> None:
    # ARCH §12.1: hypothesis ids are snake_case.
    data = a_hypotheses_file()
    data["hypotheses"][0]["id"] = "Recent Change"
    path = write_yaml_file(tmp_path, "hypotheses.yaml", data)
    with pytest.raises(ConfigError, match="hypotheses"):
        load_hypotheses(path)
