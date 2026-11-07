"""ADR-0021: the trace page. A signed-in analyst traces one object.

The page lists reviewed flows only, the trace runs as the signed-in user
(audited with their id), a refusal shows on the form, and a viewer without
the trace permission is turned away.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.bootstrap import api_context_for, load_flows, trace_object
from supportagent.entrypoints.api.app import create_app
from tests.integration.test_trace_db import _setup
from tests.unit.api_support import ADA, BOB, MARKER, SECRET, TENANT, catalog, login
from tests.unit.test_pii import RegexAnalyzer

pytestmark = pytest.mark.integration


def _client(
    tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes
) -> tuple[TestClient, FakeOIDC]:
    config = _setup(tmp_path)
    oidc = FakeOIDC()
    ctx = api_context_for(
        db,
        tenant_id=TENANT,
        catalog=catalog("payments"),
        master_key=master_key,
        clock=clock,
        ids=ids,
        identity=oidc,
        session_secret=SECRET,
        secure_cookies=False,
        config_marker=MARKER,
        flows=lambda: {k: f for k, f in load_flows(config).items() if f.reviewed},
        trace=lambda flow, oid, actor: trace_object(
            config,
            flow,
            oid,
            actor=actor,
            engine=db,
            master_key=master_key,
            pii=RegexAnalyzer(),
            explain=False,
        ),
    )
    return TestClient(create_app(ctx)), oidc


def _token(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]*)"', page)
    assert match
    return match.group(1)


def test_trace_page(tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes) -> None:
    client, oidc = _client(tmp_path, db, clock, ids, master_key)
    login(client, oidc, ADA)
    page = client.get("/ui/trace").text
    assert "Trade from submission to settlement" in page and 'href="/ui/trace"' in page
    done = client.post(
        "/ui/trace",
        data={"csrf_token": _token(page), "flow": "trade-settlement", "object_id": "T-9999"},
    )
    assert done.status_code == 200
    assert "T-9999: rejected at Enrich" in done.text
    with db.connect() as conn:
        actors = [r[0] for r in conn.execute(text("SELECT actor FROM events WHERE kind = 'trace'"))]
    assert actors and actors[0].startswith("user:")

    refused = client.post(
        "/ui/trace",
        data={"csrf_token": _token(page), "flow": "trade-settlement", "object_id": "bad id"},
    )
    assert refused.status_code == 422 and "isn&#39;t a trade id" in refused.text


def test_a_viewer_cannot_trace(
    tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes
) -> None:
    client, oidc = _client(tmp_path, db, clock, ids, master_key)
    login(client, oidc, ADA)  # the first user is the admin
    client.cookies.clear()
    login(client, oidc, BOB)
    page = client.get("/ui/trace")
    assert page.status_code == 403 and "trace permission" in page.text
