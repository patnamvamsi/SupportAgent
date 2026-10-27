"""Shared HTTP call for the REST tools: typed ``ToolError`` retryability (01 §6)."""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from supportagent.domain.errors import ToolError

_RETRYABLE = {429, 500, 502, 503, 504}


def get_json(
    client: httpx.Client,
    path: str,
    *,
    params: Mapping[str, str | int] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_s: float,
) -> object:
    try:
        resp = client.get(
            path, params=dict(params or {}), headers=dict(headers or {}), timeout=timeout_s
        )
    except httpx.HTTPError as exc:
        raise ToolError(f"GET {path}: {type(exc).__name__}", retryable=True) from exc  # noqa: TRY003
    if resp.status_code >= 400:  # noqa: PLR2004
        raise ToolError(  # noqa: TRY003
            f"GET {path} -> {resp.status_code}", retryable=resp.status_code in _RETRYABLE
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(f"GET {path}: response is not JSON") from exc  # noqa: TRY003
