"""WP-012 DoD: login works against FakeOIDC end to end, through bootstrap.

The app is built from ``bootstrap.build_api_context`` on a migrated SQLite file, with
the FakeOIDC identity provider swapped in (04 §1). The first admin logs in,
runs first-run setup, and approves the default T1 grants in one batch.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.bootstrap import build_api_context, run_migrations
from supportagent.config.schema import AppConfig
from supportagent.entrypoints.api.app import create_app
from tests.unit.api_support import ADA, csrf, login
from tests.unit.test_triage_service import _shadow_config


def _config(db_url: str) -> AppConfig:
    data = _shadow_config().model_dump(mode="json")
    data["database"] = {"url": db_url}
    data["paths"]["catalog"] = "catalog/services.example.yaml"
    for secret in ("client_id", "client_secret", "session_secret"):
        data["identity"][secret] = "e2e-" + secret + "-" + "x" * 32
    data["servicenow"]["auth"]["client_id"] = "sn-client-id"
    data["servicenow"]["auth"]["client_secret"] = "sn-client-secret"  # noqa: S105 — fake
    return AppConfig.model_validate(data)


def test_login_and_first_run_setup_e2e(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'e2e.db'}"
    run_migrations(url)
    oidc = FakeOIDC()
    ctx = build_api_context(_config(url), identity=oidc, master_key=b"\x01" * 32)
    # The config's redirect URL is https, so cookies are Secure: talk https.
    client = TestClient(create_app(ctx), base_url="https://testserver")

    login(client, oidc, ADA)
    state = client.get("/api/v1/setup").json()
    assert state["has_admin"] is True
    n_defaults = len(state["default_grants"])
    assert n_defaults > 0 and all(g["status"] == "missing" for g in state["default_grants"])

    approved = client.post("/api/v1/setup/approve", headers=csrf(client)).json()["approved"]
    assert len(approved) == n_defaults

    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status, granted_by, requested_by FROM policy_grants")
        ).all()
    engine.dispose()
    assert {r[0] for r in rows} == {"approved"}
    assert {r[1] for r in rows} == {"sub-ada"}
    assert all(r[2].startswith("config:") for r in rows)
