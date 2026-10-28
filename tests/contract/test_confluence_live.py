"""BL-051 contract: the Confluence source reads a real Cloud space, read-only.

Opt-in; needs a site, a space key, the account email and a read-only API token:

    SUPPORTAGENT_CONFLUENCE_URL=https://<site>.atlassian.net/wiki \\
    SUPPORTAGENT_CONFLUENCE_SPACE=<key> \\
    SUPPORTAGENT_CONFLUENCE_USER=<email> \\
    SUPPORTAGENT_CONFLUENCE_TOKEN=... \\
    uv run pytest -m contract tests/contract/test_confluence_live.py
"""

from __future__ import annotations

import os

import httpx
import pytest

from supportagent.adapters.knowledge.confluence import load_confluence_space, site_root
from supportagent.adapters.llm.detectors import built_in_detectors
from supportagent.adapters.llm.redact import Redactor

_ENV = (
    "SUPPORTAGENT_CONFLUENCE_URL",
    "SUPPORTAGENT_CONFLUENCE_SPACE",
    "SUPPORTAGENT_CONFLUENCE_USER",
    "SUPPORTAGENT_CONFLUENCE_TOKEN",
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.enable_socket,
    pytest.mark.skipif(not all(os.environ.get(k) for k in _ENV), reason="needs a Confluence site"),
]


def test_live_space_loads_as_cited_internal_chunks() -> None:
    redactor = Redactor(detectors=built_in_detectors())
    seen: list[str] = []

    def record(request: httpx.Request) -> None:
        seen.append(request.method)

    with httpx.Client(
        base_url=site_root(os.environ["SUPPORTAGENT_CONFLUENCE_URL"]),
        auth=httpx.BasicAuth(
            os.environ["SUPPORTAGENT_CONFLUENCE_USER"], os.environ["SUPPORTAGENT_CONFLUENCE_TOKEN"]
        ),
        headers={"Accept": "application/json"},
        event_hooks={"request": [record]},
    ) as client:
        loaded = load_confluence_space(
            client,
            space="live-wiki",
            space_key=os.environ["SUPPORTAGENT_CONFLUENCE_SPACE"],
            redact=lambda t: redactor.redact(t).text,
            max_pages=25,
        )
    assert loaded.pages > 0
    assert set(seen) == {"GET"}
    for chunk in loaded.chunks:
        assert chunk.meta.doc_id.startswith("live-wiki:confluence/")
        assert chunk.meta.url and chunk.meta.url.startswith("https://")
        assert chunk.meta.version and chunk.text.strip()
