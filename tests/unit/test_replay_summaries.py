"""Staging finds: replay summaries match triage payloads; ``/ui`` lands somewhere."""

from __future__ import annotations

from typing import Any

import pytest

from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app.replay import summarize
from supportagent.domain.events import EventKind
from tests.unit.api_support import make_client


def test_decision_summary_shows_outcome_and_reason() -> None:
    payload = {
        "v": 1,
        "outcome": "safe_default",
        "reason": "unresolved-service",
        "classification": None,
        "severity": "P4",
        "route_id": None,
    }
    assert summarize(EventKind.DECISION, payload) == (
        "outcome=safe_default reason=unresolved-service severity=P4"
    )


def test_routed_decision_names_its_route() -> None:
    payload = {
        "outcome": "routed",
        "reason": "routed",
        "classification": "database/saturation",
        "severity": "P3",
        "route_id": "db-saturation",
    }
    assert summarize(EventKind.DECISION, payload).endswith(
        "classification=database/saturation severity=P3 route=db-saturation"
    )


def test_stage_decision_summary() -> None:
    assert summarize(EventKind.DECISION, {"v": 1, "stage": "retention"}) == "stage=retention"


def test_preclass_summary_shows_the_service() -> None:
    assert summarize(EventKind.PRECLASS, {"v": 1, "service": None}) == "service=None"


@pytest.mark.parametrize("path", ["/", "/ui"])
def test_ui_root_redirects_to_the_approvals_queue(
    path: str, db: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    """Staging find: ``/ui`` was a 404; it now lands on approvals (then login)."""
    client = make_client(db, clock, ids, master_key, FakeOIDC())
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/approvals"
