"""WP-223: the catalog drafted by the instance's onboarding, accepted live (ADR-0023 §2).

Canonical test ID from 12-wp WP-223: ``test_catalog_and_detectors_are_steps`` (the
detectors half is ``test_detector_step.test_detectors_are_a_step``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.bootstrap import _catalog_view, catalog_review_for
from supportagent.domain.errors import DomainValidationError
from tests.unit.api_support import ADA, login, make_client
from tests.unit.test_detector_step import _History, _run
from tests.unit.test_in_instance_onboarding import _config

pytestmark = pytest.mark.unit


def test_catalog_and_detectors_are_steps(tmp_path: Path, db: Any, clock: Any) -> None:
    run = _run(tmp_path, db, clock, _History())
    steps = list(run["steps"])
    assert steps.index("graph") < steps.index("catalog") < steps.index("detectors")
    catalog = run["steps"]["catalog"]
    assert catalog.state == "done" and "service(s) drafted" in catalog.detail, catalog.detail
    review = catalog_review_for(_config(tmp_path), db, clock)
    drafted = {d["entry"]["name"]: d for d in review.state()["drafts"]}
    assert {"api", "db"} <= set(drafted)
    assert "api" in review.unreviewed()
    assert any(n.how == "/ui/onboarding/catalog" for n in run["needs"]), "to accept"


def test_accepted_services_join_the_catalog_without_a_restart(
    tmp_path: Path, db: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    _run(tmp_path, db, clock, _History())
    base = tmp_path / "services.yaml"
    base.write_text(
        "version: v1\nservices:\n  - name: db\n    resources: [db]\n"
        "    paging_integration: none:dba\n"
    )
    config = _config(tmp_path)
    config = config.model_copy(
        update={"paths": config.paths.model_copy(update={"catalog": str(base)})}
    )
    live = _catalog_view(config, db, clock)
    assert live.resolve_service(resource="db") == "db", "the configured catalog"
    assert live.resolve_service(resource="api") is None, "not accepted yet"

    oidc = FakeOIDC()
    http = make_client(db, clock, ids, master_key, oidc)
    login(http, oidc, ADA)
    page = http.get("/ui/onboarding/catalog").text
    assert 'name="accept:api"' in page and 'name="paging:api"' in page
    token = http.cookies["sa_csrf"]
    refused = http.post("/ui/onboarding/catalog", data={"csrf_token": token, "accept:api": "1"})
    assert refused.status_code == 422 and "paging integration" in refused.text
    saved = http.post(
        "/ui/onboarding/catalog",
        data={"csrf_token": token, "accept:api": "1", "paging:api": "none:owner"},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"].endswith("saved=1")

    clock.advance(31)
    assert live.resolve_service(resource="api") == "api", "picked up within 30 s"
    entry = live.service("api")
    assert entry is not None and entry.paging_integration == "none:owner"
    assert live.version == "v1+accepted-1" and live.service("db") is not None
    review = catalog_review_for(config, db, clock)
    assert "api" not in review.unreviewed()
    with pytest.raises(DomainValidationError, match="no drafted service"):
        review.accept({"nope": "none:x"}, actor="ada")

    review.withdraw(["api"], actor="ada")
    clock.advance(31)
    assert live.resolve_service(resource="api") is None, "withdrawn"
