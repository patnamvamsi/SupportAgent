"""BL-051: the Confluence Cloud knowledge source, against a fake of its REST API.

The fake serves the response shapes of REST v2 (spaces by key, cursor-paged
pages with storage bodies, labels) and v1 (read restrictions), and records
every request, so the tests also show the source only ever reads.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from supportagent.adapters.knowledge.confluence import load_confluence_space, site_root
from supportagent.adapters.knowledge.storage_html import storage_to_markdown
from supportagent.adapters.llm.detectors import built_in_detectors
from supportagent.adapters.llm.redact import Redactor
from supportagent.bootstrap import build_knowledge_index
from supportagent.config.loader import load_config, load_config_offline
from supportagent.config.schema import ConfluenceSource
from supportagent.domain.errors import ConfigError
from supportagent.domain.knowledge import DocScope
from tests.unit.test_lab import _config as lab_config

_REDACTOR = Redactor(detectors=built_in_detectors())
_SITE = "https://example-wiki.atlassian.net"
_TOKEN = "fake-token"  # noqa: S105 — a fixture value, not a secret


def _redact(text: str) -> str:
    return _REDACTOR.redact(text).text


def _page(page_id: str, title: str, body: str, *, version: int = 3) -> dict[str, Any]:
    return {
        "id": page_id,
        "title": title,
        "version": {"number": version, "createdAt": "2026-09-14T08:30:00.000Z"},
        "body": {"storage": {"value": body, "representation": "storage"}},
        "_links": {"webui": f"/spaces/OPS/pages/{page_id}/{title.replace(' ', '+')}"},
    }


_PAGES = [
    _page(
        "101",
        "Lock contention",
        "<h1>Symptoms</h1><p>Writes stall and <code>pg_locks</code> shows waiters.</p>"
        "<h2>Contact</h2><p>Page jane.doe@example.com before killing a backend.</p>",
    ),
    _page("102", "Customer FAQ", "<p>Payroll exports run nightly at 02:00.</p>"),
    _page("103", "Restricted FAQ", "<p>Internal escalation steps.</p>"),
    _page("104", "Too many guesses", "<p>Everything is DNS.</p>"),
]
_LABELS = {
    "101": ["hypothesis-pg_lock_contention", "service-payroll-api", "postgres"],
    "102": ["visibility-customer"],
    "103": ["visibility-customer"],
    "104": [f"hypothesis-h{i}" for i in range(6)],
}
_RESTRICTED = {"103"}


class FakeConfluence:
    """REST v2 + the v1 restriction read, two pages per cursor page."""

    def __init__(self, *, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "Unauthorized"})
        path = request.url.path
        if path == "/wiki/api/v2/spaces":
            key = request.url.params.get("keys")
            return httpx.Response(200, json={"results": [{"id": "9001"}] if key == "OPS" else []})
        if path == "/wiki/api/v2/spaces/9001/pages":
            start = int(request.url.params.get("cursor", "0"))
            batch = _PAGES[start : start + 2]
            links: dict[str, str] = {}
            if start + 2 < len(_PAGES):
                links["next"] = f"/wiki/api/v2/spaces/9001/pages?cursor={start + 2}&limit=2"
            return httpx.Response(200, json={"results": batch, "_links": links})
        if path.startswith("/wiki/api/v2/pages/") and path.endswith("/labels"):
            page_id = path.split("/")[5]
            return httpx.Response(200, json={"results": [{"name": n} for n in _LABELS[page_id]]})
        if path.endswith("/restriction/byOperation/read"):
            page_id = path.split("/")[5]
            users = [{"accountId": "a1"}] if page_id in _RESTRICTED else []
            return httpx.Response(
                200, json={"restrictions": {"user": {"results": users}, "group": {"results": []}}}
            )
        return httpx.Response(404)


def _load(fake: FakeConfluence, *, max_pages: int = 50) -> Any:
    with httpx.Client(base_url=_SITE, transport=httpx.MockTransport(fake)) as client:
        return load_confluence_space(
            client, space="ops-wiki", space_key="OPS", redact=_redact, max_pages=max_pages
        )


def test_pages_become_docs_with_provenance_and_label_scope() -> None:
    fake = FakeConfluence()
    loaded = _load(fake)
    docs = {c.meta.doc_id: c.meta for c in loaded.chunks}
    lock = docs["ops-wiki:confluence/101"]
    assert lock.title == "Lock contention" and lock.version == "3"
    assert lock.last_updated == date(2026, 9, 14)
    assert lock.url == f"{_SITE}/wiki/spaces/OPS/pages/101/Lock+contention"
    assert lock.hypotheses == ("pg_lock_contention",) and lock.services == ("payroll-api",)
    assert lock.labels == ("postgres",) and lock.visibility == "internal"
    headings = [c.heading for c in loaded.chunks if c.meta.doc_id == lock.doc_id]
    assert headings == ["Symptoms", "Contact"]
    # Both cursor pages were read: four pages, one skipped (below).
    assert loaded.pages == 4 and not loaded.truncated
    assert {r.method for r in fake.requests} == {"GET"}


def test_customer_visibility_needs_the_label_and_no_read_restrictions() -> None:
    docs = {c.meta.doc_id: c.meta for c in _load(FakeConfluence()).chunks}
    assert docs["ops-wiki:confluence/102"].visibility == "customer"
    assert docs["ops-wiki:confluence/103"].visibility == "internal"


def test_restrictions_that_cannot_be_read_keep_a_page_internal() -> None:
    fake = FakeConfluence()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/restriction/byOperation/read"):
            return httpx.Response(403)
        return fake(request)

    with httpx.Client(base_url=_SITE, transport=httpx.MockTransport(handler)) as client:
        loaded = load_confluence_space(
            client, space="ops-wiki", space_key="OPS", redact=_redact, max_pages=50
        )
    assert {c.meta.visibility for c in loaded.chunks} == {"internal"}


def test_a_page_with_invalid_labels_is_skipped_not_fatal() -> None:
    loaded = _load(FakeConfluence())
    assert [page for page, _ in loaded.skipped] == ["104"]
    assert "hypotheses" in loaded.skipped[0][1]
    assert "ops-wiki:confluence/104" not in {c.meta.doc_id for c in loaded.chunks}


def test_page_text_is_redacted_before_chunking() -> None:
    text = " ".join(c.text for c in _load(FakeConfluence()).chunks)
    assert "jane.doe@example.com" not in text and "pg_locks" in text


def test_max_pages_caps_the_read() -> None:
    fake = FakeConfluence()
    loaded = _load(fake, max_pages=1)
    assert loaded.pages == 1 and loaded.truncated
    assert sum(r.url.path.endswith("/pages") for r in fake.requests) == 1


def test_unknown_space_and_auth_failures_are_config_errors() -> None:
    fake = httpx.MockTransport(FakeConfluence())
    with httpx.Client(base_url=_SITE, transport=fake) as client, pytest.raises(ConfigError):
        load_confluence_space(client, space="x-wiki", space_key="NOPE", redact=_redact, max_pages=5)
    with pytest.raises(ConfigError, match="API token"):
        _load(FakeConfluence(status=401))


def test_storage_format_to_markdown() -> None:
    storage = (
        "<h1>Restart</h1><p>Run <strong>this</strong>, see <a href='https://x'>docs</a>.</p>"
        "<ul><li>one</li><li>two</li></ul>"
        "<table><tbody><tr><th>Step</th><th>Owner</th></tr><tr><td>1</td><td>ops</td></tr>"
        "</tbody></table>"
        '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">sql'
        "</ac:parameter><ac:plain-text-body><![CDATA[SELECT 1;]]></ac:plain-text-body>"
        "</ac:structured-macro><h2>After</h2><p>a<br/>b &amp; c</p>"
    )
    md = storage_to_markdown(storage)
    assert md.startswith("# Restart\n\nRun this, see docs.")
    assert "- one\n- two" in md and "Step | Owner\n1 | ops" in md
    assert "```\nSELECT 1;\n```" in md and "sql" not in md and "https://x" not in md
    assert "## After\n\na\nb & c" in md


def test_site_root_accepts_the_wiki_url() -> None:
    assert site_root(f"{_SITE}/wiki/") == _SITE == site_root(_SITE)


# --- config and bootstrap ------------------------------------------------------------


def _source(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "kind": "confluence",
        "space": "ops-wiki",
        "base_url": f"{_SITE}/wiki",
        "space_key": "OPS",
        "user": "analyst@example.com",
        "token": _TOKEN,
    }
    values.update(overrides)
    return values


def _with_sources(*sources: Mapping[str, Any]) -> Any:
    config = lab_config()
    knowledge = config.knowledge.model_validate({"sources": list(sources)})
    return config.model_copy(update={"knowledge": knowledge})


def test_index_from_config_scopes_confluence_docs_by_service() -> None:
    fake = FakeConfluence()
    index = build_knowledge_index(_with_sources(_source()), transport=httpx.MockTransport(fake))
    assert index is not None
    auth = fake.requests[0].headers["Authorization"]
    assert auth.startswith("Basic ")
    scope = DocScope(spaces=frozenset({"ops-wiki"}), service="payroll-api", audience="internal")
    hits = index.search("writes stall locks", scope, k=3, today=date(2026, 10, 28))
    assert hits and hits[0].chunk.meta.doc_id == "ops-wiki:confluence/101"
    other = DocScope(spaces=frozenset({"ops-wiki"}), service="billing", audience="internal")
    assert all(
        h.chunk.meta.doc_id != "ops-wiki:confluence/101"
        for h in index.search("writes stall locks", other, k=3, today=date(2026, 10, 28))
    )


def test_unreachable_wiki_degrades_at_runtime_and_fails_doctor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _with_sources(_source())
    down = httpx.MockTransport(FakeConfluence(status=503))
    with caplog.at_level(logging.WARNING, logger="supportagent.bootstrap"):
        index = build_knowledge_index(config, transport=down)
    assert index is not None and index.chunks == []
    assert "ops-wiki" in caplog.text and _TOKEN not in caplog.text
    with pytest.raises(ConfigError, match="503"):
        build_knowledge_index(config, transport=down, strict=True)


_YAML = Path("supportagent.example.yaml")


def test_loader_resolves_the_source_secrets(tmp_path: Path) -> None:
    text = _YAML.read_text(encoding="utf-8")
    path = tmp_path / "sa.yaml"
    path.write_text(
        text
        + "\nknowledge:\n  sources:\n"
        + "    - {kind: confluence, space: ops-wiki, base_url: 'https://example-wiki.atlassian.net',"
        + " space_key: OPS, user_env: CONF_USER, token_env: CONF_TOKEN}\n",
        encoding="utf-8",
    )
    env = {
        "SUPPORTAGENT_DB_URL": "sqlite:///x.db",
        "SN_CLIENT_ID": "x",
        "SN_CLIENT_SECRET": "x",
        "OIDC_CLIENT_ID": "x",
        "OIDC_CLIENT_SECRET": "x",
        "SA_SESSION_SECRET": "x" * 32,
        "ANTHROPIC_API_KEY": "x",
        "PAGERDUTY_TOKEN": "x",
        "CONF_USER": "analyst@example.com",
        "CONF_TOKEN": _TOKEN,
    }
    [source] = load_config(path, env).knowledge.sources
    assert isinstance(source, ConfluenceSource)
    assert source.token == _TOKEN and source.user == "analyst@example.com"
    with pytest.raises(ConfigError, match="CONF_TOKEN"):
        load_config(path, {k: v for k, v in env.items() if k != "CONF_TOKEN"})
    [offline] = load_config_offline(path).knowledge.sources
    assert isinstance(offline, ConfluenceSource) and offline.token != _TOKEN
