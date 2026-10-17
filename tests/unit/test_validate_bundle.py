"""WP-002: cross-file bundle validation (02 §1, ARCH §12).

Canonical test ID from 10-wp §WP-002:
``test_validate_bundle_reports_all_issues``.

DoD (10-wp §WP-002): ``supportagent.example.yaml`` plus the example catalog
validate cleanly — enforced by ``test_example_bundle_validates``.
"""

from __future__ import annotations

from pathlib import Path

from supportagent.adapters.files.catalog_loader import load_catalog
from supportagent.adapters.files.taxonomy_loader import load_classification, load_hypotheses
from supportagent.config.loader import load_config
from supportagent.config.validation import validate_bundle
from supportagent.domain.model import Catalog, ClassificationTaxonomy, HypothesisTaxonomy
from tests.builders import SECRET_ENV, a_catalog_file, a_config_file
from tests.yaml_io import write_yaml_file

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_validate_bundle_reports_all_issues(tmp_path: Path) -> None:
    # 02 §1: validate_bundle reports EVERY issue — unknown routing labels and
    # never-executable kinds on (different) rows are all listed, not just the
    # first.
    data = a_config_file()
    data["routing"] = [
        {
            "id": "bad-row-1",
            "match": {"classification": "database/saturation", "severity": ["P1"]},
            "actions": ["work_note"],
            "rca": "none",
        },
        {
            "id": "bad-row-2",
            "match": {"classification": "network/ghost", "severity": ["P2"]},
            "actions": ["create_page"],  # NEVER_EXECUTABLE_KINDS (01 §4)
            "rca": "none",
        },
    ]
    data["default"] = None
    path = write_yaml_file(tmp_path, "config.yaml", data)
    cfg = load_config(path, SECRET_ENV)
    catalog = Catalog(**a_catalog_file())
    # A taxonomy that defines neither routing label.
    taxonomy = ClassificationTaxonomy.model_validate(
        {
            "version": "1.0.0",
            "labels": [{"id": "deploy/failure", "description": "A release failed or is stuck."}],
        }
    )
    hypotheses = HypothesisTaxonomy.model_validate(
        {"version": "1.0.0", "hypotheses": [{"id": "recent_change_push", "excludes": []}]}
    )
    issues = validate_bundle(cfg, catalog, taxonomy, hypotheses)
    codes = [issue.code for issue in issues]
    assert codes.count("routing.label-unknown") == 2  # both rows, not just the first
    assert "routing.kind-never-executable" in codes
    for issue in issues:
        assert issue.message  # every issue carries a human-readable message
        assert issue.path  # ... and a stable location for doctor output


def test_example_bundle_validates() -> None:
    # DoD (10-wp §WP-002): the example config plus the example catalog and the
    # seed taxonomies validate cleanly.
    cfg = load_config(REPO_ROOT / "supportagent.example.yaml", SECRET_ENV)
    catalog = load_catalog(REPO_ROOT / "catalog/services.example.yaml")
    taxonomy = load_classification(REPO_ROOT / "taxonomy/classification.yaml")
    hypotheses = load_hypotheses(REPO_ROOT / "taxonomy/hypotheses.yaml")
    assert validate_bundle(cfg, catalog, taxonomy, hypotheses) == []
