"""Shared HTTP calls for the paging providers: typed retryability (01 §6)."""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from supportagent.domain.errors import ExternalServiceError

_RETRYABLE = {429, 500, 502, 503, 504}
TIMEOUT_S = 10.0


def call_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    params: Mapping[str, str | list[str]] | None = None,
    body: Mapping[str, object] | None = None,
) -> object:
    try:
        resp = client.request(method, path, params=dict(params or {}), json=body, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise ExternalServiceError(  # noqa: TRY003
            f"{method} {path}: {type(exc).__name__}", retryable=True
        ) from exc
    if resp.status_code == 404:  # noqa: PLR2004
        return None
    if resp.status_code >= 400:  # noqa: PLR2004
        retry_after = resp.headers.get("retry-after")
        raise ExternalServiceError(  # noqa: TRY003
            f"{method} {path} -> {resp.status_code}",
            retryable=resp.status_code in _RETRYABLE,
            retry_after_s=float(retry_after) if retry_after and retry_after.isdigit() else None,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ExternalServiceError(f"{method} {path}: response is not JSON") from exc  # noqa: TRY003
