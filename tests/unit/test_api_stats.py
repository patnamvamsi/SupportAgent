"""WP-014: ``GET /api/v1/stats`` and ``/ui/stats`` (02 §2, §3; BL-025)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from supportagent.adapters.fakes.oidc import FakeOIDC
from tests.unit.api_support import CARL, login, logout, make_client, seed_users
from tests.unit.test_stats import _shadow


@pytest.fixture
def oidc() -> FakeOIDC:
    return FakeOIDC()


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes, oidc: FakeOIDC) -> TestClient:
    return make_client(db, clock, ids, master_key, oidc)


def _as_viewer(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    from tests.unit.api_support import ADA  # noqa: PLC0415

    login(client, oidc, ADA)
    logout(client)
    seed_users(db, ("sub-carl", "viewer"))
    login(client, oidc, CARL)


def test_api_stats_shadow_report(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    _shadow(db, "s1", first=1_790_000_030, agreement=1)
    _shadow(db, "s2", reason="llm-failure", agreement=0)
    _as_viewer(client, oidc, db)
    body = client.get("/api/v1/stats", params={"mode": "shadow", "window": "all"}).json()
    assert body["mode"] == "shadow"
    assert body["n_cases"] == 2
    assert body["agreement"]["n"] == 2 and body["agreement"]["agreed"] == 1
    assert body["safe_default_by_reason"] == {"llm-failure": 1}
    assisted = client.get("/api/v1/stats", params={"mode": "assisted", "window": "all"}).json()
    assert assisted["mode"] == "assisted" and assisted["n_cases"] == 0


def test_api_stats_rejects_bad_window_and_mode(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    _as_viewer(client, oidc, db)
    assert client.get("/api/v1/stats", params={"window": "3w"}).status_code == 422
    assert client.get("/api/v1/stats", params={"mode": "standard"}).status_code == 422


def test_ui_stats_page(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    _shadow(db, "s1", first=1_790_000_030, agreement=1)
    _as_viewer(client, oidc, db)
    page = client.get("/ui/stats", params={"window": "all"}).text
    assert "Shadow report" in page
    assert "Wilson 95% lower bound" in page
    assert "zero by construction" in page
    assert "Assisted report" in client.get("/ui/stats?mode=assisted&window=all").text
