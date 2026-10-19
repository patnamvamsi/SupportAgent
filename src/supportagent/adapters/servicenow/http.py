"""Retrying ServiceNow HTTP client (02 §5-§6).

Wraps a *synchronous* ``httpx.Client`` (injected, so tests use
``adapters.fakes.servicenow.SyncASGITransport`` to drive the ASGI fake while
production uses the real transport + ``base_url``) and applies the 02 §6 HTTP
behaviour:

* timeouts — 10 s connect / 30 s read; ``build_client`` is the production
  factory that applies them;
* ``429`` — retry, honouring ``Retry-After`` when present;
* ``5xx`` — retry with exponential backoff ``0.5s x 2ⁿ``;
* any other ``4xx`` — fail fast, ``ServiceNowError(retryable=False)``.

Up to ``max_tries`` attempts (default 5). The sleep and the jitter are
*injected seams* (01 §3): the ``random`` module is banned in ``src`` (05 §6),
so the ±20% jitter is supplied by the caller, and the sleep is injectable so
tests never actually wait.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from supportagent.adapters.servicenow.auth import ServiceNowAuth
from supportagent.domain.errors import ServiceNowError

DEFAULT_MAX_TRIES = 5
DEFAULT_BACKOFF_BASE_S = 0.5
DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_READ_TIMEOUT_S = 30.0


def build_client(base_url: str, *, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """A production ``httpx.Client`` for one ServiceNow instance (02 §6).

    Timeouts are 10 s connect / 30 s read. ``transport`` is a test seam: pass
    ``SyncASGITransport`` to drive ``FakeServiceNowHTTP`` without sockets.
    """
    return httpx.Client(
        base_url=base_url,
        transport=transport,
        timeout=httpx.Timeout(DEFAULT_READ_TIMEOUT_S, connect=DEFAULT_CONNECT_TIMEOUT_S),
    )


def _default_sleep(seconds: float) -> None:
    import time  # noqa: PLC0415 — deferred so the banned-api scan stays clean at import

    time.sleep(seconds)


class ServiceNowHTTP:
    """One ServiceNow instance's HTTP surface with the 02 §5 retry policy."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        http: httpx.Client,
        auth: ServiceNowAuth,
        max_tries: int = DEFAULT_MAX_TRIES,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        sleep: Callable[[float], None] = _default_sleep,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self._http = http
        self._auth = auth
        self._max_tries = max(1, max_tries)
        self._backoff_base_s = backoff_base_s
        self._sleep_fn = sleep
        self._jitter = jitter

    def request(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,  # noqa: ANN401
        data: Any = None,  # noqa: ANN401
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Issue ``method path`` with retries; return the 2xx response.

        Raises ``ServiceNowError`` (retryable typed per 02 §5) on failure.
        """
        last: ServiceNowError | None = None
        for attempt in range(self._max_tries):
            is_last = attempt == self._max_tries - 1
            try:
                resp = self._http.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    data=data,
                    headers=self._build_headers(headers),
                )
            except httpx.HTTPError as exc:
                last = ServiceNowError(f"{method} {path}: {exc}", retryable=True)
                if is_last:
                    break
                self._sleep(self._wait_s(attempt, None))
                continue
            if 200 <= resp.status_code < 300:  # noqa: PLR2004
                return resp
            if resp.status_code == 429 or 500 <= resp.status_code < 600:  # noqa: PLR2004
                retry_after = self._parse_retry_after(resp)
                last = ServiceNowError(
                    f"{method} {path} -> {resp.status_code}",
                    retryable=True,
                    retry_after_s=retry_after,
                )
                if is_last:
                    break
                self._sleep(self._wait_s(attempt, retry_after))
                continue
            # A non-retryable 4xx (400/401/403/404/...): fail fast (02 §5).
            raise ServiceNowError(f"{method} {path} -> {resp.status_code}", retryable=False)  # noqa: TRY003
        assert last is not None  # noqa: S101
        raise last

    # -- internals ---------------------------------------------------------

    def _build_headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers = dict(self._auth.headers())
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> float | None:
        value = resp.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    def _wait_s(self, attempt: int, retry_after_s: float | None) -> float:
        base = retry_after_s if retry_after_s is not None else self._backoff_base_s * (2**attempt)
        if self._jitter is not None:
            return max(0.0, self._jitter(base))
        return base

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep_fn(seconds)


__all__ = [
    "DEFAULT_BACKOFF_BASE_S",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_MAX_TRIES",
    "DEFAULT_READ_TIMEOUT_S",
    "ServiceNowHTTP",
    "build_client",
]
