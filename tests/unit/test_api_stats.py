"""WP-014: ``GET /api/v1/stats`` and ``/ui/stats`` (02 §2, §3; BL-025)."""

from __future__ import annotations

import json
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


def _rca(db: Any, case_id: str, *tags: list[str]) -> None:
    from sqlalchemy import text  # noqa: PLC0415

    claims = [
        {"text": f"claim {i}", "cites": [], "tags": t, "sendable": False}
        for i, t in enumerate(tags)
    ]
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rca_artifacts (id, tenant_id, case_id, outcome, artifact_json, "
                "created_ts) VALUES (:id, 'acme', :c, 'SUPPORTED', :a, 1790000000)"
            ),
            {"id": f"rca_{case_id}", "c": case_id, "a": json.dumps({"claims": claims})},
        )


def test_ui_stats_shows_what_readiness_held_back(
    client: TestClient, oidc: FakeOIDC, db: Any
) -> None:
    """WP-A6: claims held back by readiness, and the calibration in use."""
    _shadow(db, "s1", reason="class-not-ready")
    _shadow(db, "s2", first=1_790_000_030)
    _rca(db, "case_s1", ["not_ready"], ["not_ready", "speculation"], [])
    _rca(db, "case_s2", [])
    _as_viewer(client, oidc, db)
    page = client.get("/ui/stats", params={"window": "all"}).text
    assert "2 claim(s) in 1 of 2 RCA case(s)" in page
    assert "Routed safe: class not ready</span><span>1 case(s)" in page
    assert "Calibration in use" not in page  # not wired in this context


def test_calibration_source_names_the_table(db: Any, clock: Any) -> None:
    from pathlib import Path  # noqa: PLC0415

    from supportagent.bootstrap import calibration_source  # noqa: PLC0415
    from supportagent.config.loader import load_config_offline  # noqa: PLC0415

    cfg = load_config_offline(Path(__file__).resolve().parents[2] / "evals/lab/lab.yaml")
    assert calibration_source(cfg, db, clock) == ("artifact", "nothing fitted yet")


def test_ui_stats_names_the_calibration_in_use(
    db: Any, clock: Any, ids: Any, master_key: bytes, oidc: FakeOIDC
) -> None:
    import dataclasses  # noqa: PLC0415

    from supportagent.entrypoints.api.app import create_app  # noqa: PLC0415

    client = make_client(db, clock, ids, master_key, oidc)
    ctx = dataclasses.replace(
        client.app.state.ctx,  # type: ignore[attr-defined]
        calibration_in_use=lambda: ("artifact", "fitted with qwen, triage uses claude"),
    )
    client = TestClient(create_app(ctx))
    _as_viewer(client, oidc, db)
    page = client.get("/ui/stats", params={"window": "all"}).text
    assert "the configured artifact" in page and "fitted with qwen, triage uses claude" in page
