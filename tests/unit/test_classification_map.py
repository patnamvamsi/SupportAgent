"""BL-035: ServiceNow category -> taxonomy label for outcome feedback and shadow agreement."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from supportagent.adapters.files.catalog_loader import load_catalog
from supportagent.adapters.files.taxonomy_loader import load_classification, load_hypotheses
from supportagent.config.loader import load_config
from supportagent.config.schema import ServiceNow
from supportagent.config.validation import validate_bundle
from supportagent.domain.classification import map_servicenow_classification
from tests.builders import SECRET_ENV

REPO = Path(__file__).resolve().parents[2]
MAP = {"db/pool": "database/saturation", "network": "network/dns"}


def test_no_map_keeps_the_lowercased_category_pair() -> None:
    assert map_servicenow_classification("Database", "Saturation", {}) == "database/saturation"
    assert map_servicenow_classification("Database", None, {}) is None


def test_map_prefers_the_most_specific_key() -> None:
    assert map_servicenow_classification("DB", "Pool", MAP) == "database/saturation"
    assert map_servicenow_classification(" Network ", "Latency", MAP) == "network/dns"
    assert map_servicenow_classification("Network", None, MAP) == "network/dns"


def test_unmapped_category_is_not_counted() -> None:
    # None, not the raw pair: agreement stays None instead of a false disagreement.
    assert map_servicenow_classification("Hardware", "Disk", MAP) is None
    assert map_servicenow_classification(None, None, MAP) is None


def _sn(classification_map: dict[str, str]) -> ServiceNow:
    return ServiceNow.model_validate(
        {
            "instance_url": "https://acme.service-now.com",
            "auth": {"kind": "basic", "user": "svc", "password": "pw-fake"},
            "integration_user": "svc",
            "tables": ["incident"],
            "poll_interval_s": 60,
            "overlap_s": 120,
            "page_size": 200,
            "qps_limit": 5,
            "classification_map": classification_map,
        }
    )


def test_map_keys_normalized_and_checked() -> None:
    assert _sn({"DB / Pool": "database/saturation"}).classification_map == {
        "db/pool": "database/saturation"
    }
    for bad in ({"a/b/c": "x/y"}, {"/pool": "x/y"}, {"DB/Pool": "x/y", "db/pool": "x/y"}):
        with pytest.raises(ValidationError):
            _sn(bad)


def test_map_labels_must_exist_in_the_taxonomy(tmp_path: Path) -> None:
    text = (REPO / "supportagent.example.yaml").read_text(encoding="utf-8")
    text = text.replace(
        "classification_map: {}",
        'classification_map: {"DB/Pool": database/saturation, "Disk": hardware/gremlins}',
    )
    path = tmp_path / "supportagent.yaml"
    path.write_text(text, encoding="utf-8")
    cfg = load_config(path, SECRET_ENV)
    issues = validate_bundle(
        cfg,
        load_catalog(REPO / "catalog/services.example.yaml"),
        load_classification(REPO / "taxonomy/classification.yaml"),
        load_hypotheses(REPO / "taxonomy/hypotheses.yaml"),
    )
    assert [(i.code, i.path) for i in issues] == [
        ("servicenow.map-label-unknown", "servicenow.classification_map.disk")
    ]
