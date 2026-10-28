"""The Confluence Cloud knowledge source (BL-051, ADR-0011 §4): pages as guidance.

Read-only REST v2 calls with an API token (basic auth, the account's email
and token): the space by key, then its current pages with their storage
bodies (cursor-paged, capped at ``max_pages``), then each page's labels. A
page becomes one doc, ``<space>:confluence/<page id>``, whose version is the
page version and whose last-updated date is that version's date.

Labels play the role of the Git source's front matter:

- ``hypothesis-<id>``: a taxonomy hypothesis the page suggests testing;
- ``service-<name>``: the page applies to that catalog service only;
- ``visibility-customer``: the page may answer a customer audience, but only
  when it has no read restrictions (a restricted page, or one whose
  restrictions can't be read, stays ``internal``);
- any other label is kept as a plain label.

A page whose labels don't validate (more than five hypotheses, say) is
skipped and reported, not fatal: a wiki edit must not take the knowledge
layer down. Bodies are converted to markdown, then redacted before chunking
and hashing (INV-06), like every other source.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx
from pydantic import ValidationError

from supportagent.adapters.knowledge.storage_html import storage_to_markdown
from supportagent.domain.errors import ConfigError
from supportagent.domain.knowledge import DocChunk, chunk_document
from supportagent.domain.model.knowledge import DocFrontMatter

_API = "/wiki/api/v2"
_PAGE_SIZE = 50
_HYPOTHESIS = "hypothesis-"
_SERVICE = "service-"
_CUSTOMER = "visibility-customer"


@dataclass(frozen=True)
class ConfluenceLoad:
    chunks: list[DocChunk]
    pages: int
    #: ``(page id, reason)`` for each page left out.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: True when ``max_pages`` stopped the read before the space's last page.
    truncated: bool = False


def site_root(base_url: str) -> str:
    """``https://x.atlassian.net`` from the site URL, with or without ``/wiki``."""
    root = base_url.rstrip("/")
    return root.removesuffix("/wiki")


def load_confluence_space(  # noqa: PLR0913
    client: httpx.Client,
    *,
    space: str,
    space_key: str,
    redact: Callable[[str], str],
    max_pages: int,
    timeout_s: float = 15.0,
) -> ConfluenceLoad:
    """Every current page of ``space_key`` (up to ``max_pages``), chunked as ``space``."""
    found = _get(client, f"{_API}/spaces", {"keys": space_key}, timeout_s)
    results = _results(found)
    if not results:
        raise ConfigError(f"knowledge source {space!r}: Confluence space {space_key!r} not found")  # noqa: TRY003
    space_id = str(results[0].get("id"))
    chunks: list[DocChunk] = []
    skipped: list[tuple[str, str]] = []
    pages = 0
    truncated = False
    path: str | None = f"{_API}/spaces/{space_id}/pages"
    params: dict[str, str | int] | None = {
        "body-format": "storage",
        "status": "current",
        "limit": _PAGE_SIZE,
    }
    while path is not None:
        batch = _get(client, path, params, timeout_s)
        for page in _results(batch):
            if pages >= max_pages:
                truncated = True
                break
            pages += 1
            page_id = str(page.get("id"))
            try:
                chunks.extend(
                    _page_chunks(client, page, space=space, redact=redact, timeout_s=timeout_s)
                )
            except _SkipPageError as exc:
                skipped.append((page_id, str(exc)))
        if truncated:
            break
        # The next cursor is a site-relative link carrying its own query.
        path, params = _next_link(batch), None
    return ConfluenceLoad(chunks, pages, skipped, truncated)


class _SkipPageError(Exception):
    pass


def _page_chunks(
    client: httpx.Client,
    page: Mapping[str, object],
    *,
    space: str,
    redact: Callable[[str], str],
    timeout_s: float,
) -> list[DocChunk]:
    page_id = str(page.get("id"))
    labels = [
        str(item.get("name"))
        for item in _results(_get(client, f"{_API}/pages/{page_id}/labels", None, timeout_s))
    ]
    hypotheses = [lb.removeprefix(_HYPOTHESIS) for lb in labels if lb.startswith(_HYPOTHESIS)]
    services = [lb.removeprefix(_SERVICE) for lb in labels if lb.startswith(_SERVICE)]
    plain = [lb for lb in labels if not lb.startswith((_HYPOTHESIS, _SERVICE)) and lb != _CUSTOMER]
    customer = _CUSTOMER in labels and not _restricted(client, page_id, timeout_s)
    version = _mapping(page.get("version"))
    links = _mapping(page.get("_links"))
    webui = links.get("webui")
    front = {
        "title": str(page.get("title") or page_id),
        "url": _page_url(client, webui),
        "version": str(version.get("number")) if version.get("number") is not None else None,
        "last_updated": _date(version.get("createdAt")),
        "visibility": "customer" if customer else "internal",
        "services": services,
        "labels": plain,
        "hypotheses": hypotheses,
    }
    try:
        matter = DocFrontMatter.model_validate(front)
    except ValidationError as exc:
        reasons = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise _SkipPageError(reasons) from exc
    body = _mapping(_mapping(page.get("body")).get("storage")).get("value")
    markdown = storage_to_markdown(body if isinstance(body, str) else "")
    meta = matter.meta(doc_id=f"{space}:confluence/{page_id}", space=space)
    return chunk_document(meta, markdown, redact=redact)


def _page_url(client: httpx.Client, webui: object) -> str | None:
    root = str(client.base_url).rstrip("/")
    return f"{root}/wiki{webui}" if isinstance(webui, str) else None


def _restricted(client: httpx.Client, page_id: str, timeout_s: float) -> bool:
    """Whether the page has read restrictions; ``True`` when they can't be read."""
    try:
        found = _get(
            client,
            f"/wiki/rest/api/content/{page_id}/restriction/byOperation/read",
            None,
            timeout_s,
        )
    except ConfigError:
        return True
    restrictions = _mapping(_mapping(found).get("restrictions"))
    return any(_results(restrictions.get(kind)) for kind in ("user", "group"))


def _get(
    client: httpx.Client,
    path: str,
    params: Mapping[str, str | int] | None,
    timeout_s: float,
) -> Mapping[str, object]:
    try:
        resp = client.get(path, params=dict(params) if params else None, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise ConfigError(f"Confluence GET {path}: {type(exc).__name__}") from exc  # noqa: TRY003
    if resp.status_code >= 400:  # noqa: PLR2004
        hint = " (check the account email and API token)" if resp.status_code in {401, 403} else ""
        raise ConfigError(f"Confluence GET {path} -> {resp.status_code}{hint}")  # noqa: TRY003
    try:
        data: object = resp.json()
    except ValueError as exc:
        raise ConfigError(f"Confluence GET {path}: response is not JSON") from exc  # noqa: TRY003
    return _mapping(data)


def _next_link(batch: Mapping[str, object]) -> str | None:
    nxt = _mapping(batch.get("_links")).get("next")
    return nxt if isinstance(nxt, str) and nxt.startswith("/wiki/") else None


def _results(data: object) -> list[Mapping[str, object]]:
    items = _mapping(data).get("results")
    return [i for i in items if isinstance(i, Mapping)] if isinstance(items, list) else []


def _mapping(data: object) -> Mapping[str, object]:
    return data if isinstance(data, Mapping) else {}


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


__all__ = ["ConfluenceLoad", "load_confluence_space", "site_root"]
