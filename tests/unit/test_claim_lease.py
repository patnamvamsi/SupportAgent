"""BL-001: the outbox claim lease outlasts the sink's worst-case reconcile + send.

A lease shorter than one send lets a second worker reclaim a row that is
still mid-send, and both write. The write side also makes one HTTP attempt
per try: ``RetryingTicketSink`` owns the retries, and it never repeats an
ambiguous journal POST.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from supportagent.adapters.fakes.clock import FrozenClock
from supportagent.adapters.servicenow.auth import BasicAuth
from supportagent.adapters.servicenow.http import ServiceNowHTTP, build_client
from supportagent.app.outbox import DEFAULT_LEASE_S, OutboxExecutor
from supportagent.bootstrap import build_ticket_sink, outbox_claim_budget_s, outbox_claim_lease_s
from supportagent.domain.errors import ConfigError, ServiceNowError
from supportagent.ports.tickets import JournalField, SnTable
from tests.integration.test_shadow_worker import _config

_AUTH = BasicAuth(user="svc", password="pw-fake")  # noqa: S106 — fake


def _http(transport: httpx.BaseTransport, **kw: Any) -> ServiceNowHTTP:
    return ServiceNowHTTP(
        http=build_client("https://acme.service-now.com", transport=transport), auth=_AUTH, **kw
    )


def test_http_worst_case_is_bounded() -> None:
    ok = httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    # 5 tries x (10 s connect + 30 s read) + 4 waits x 30 s x 1.2 jitter headroom.
    assert _http(ok).worst_case_s == pytest.approx(5 * 40 + 4 * 36)
    assert _http(ok, max_tries=1).worst_case_s == pytest.approx(40)


def test_retry_after_is_capped() -> None:
    waits: list[float] = []
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3600"})
        return httpx.Response(200, json={})

    _http(httpx.MockTransport(handler), sleep=waits.append).request("GET", "/x")
    assert waits == [30.0]


def test_claim_lease_exceeds_the_send_budget(tmp_path: Path) -> None:
    config, clock = _config(tmp_path), FrozenClock(start=datetime(2026, 10, 21, tzinfo=UTC))
    budget = outbox_claim_budget_s(config, clock=clock)
    # reconcile: 3 x 40 + 2 x 30; send (update_fields, read + patch): 3 x 80 + 2 x 30.
    assert budget == pytest.approx(180 + 300)
    lease = outbox_claim_lease_s(config, clock=clock)
    assert lease > budget > DEFAULT_LEASE_S


def test_executor_refuses_a_lease_shorter_than_the_budget() -> None:
    ports: dict[str, Any] = dict.fromkeys(("uow", "repo", "clock", "ids", "events", "sink"))
    with pytest.raises(ConfigError, match="BL-001"):
        OutboxExecutor(**ports, handlers={}, lease_s=60, send_budget_s=480.0)
    OutboxExecutor(**ports, handlers={}, lease_s=540, send_budget_s=480.0)


def test_journal_post_is_never_repeated_after_a_5xx(tmp_path: Path) -> None:
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method in {"PATCH", "POST"}:
            posts["n"] += 1
            return httpx.Response(503)
        return httpx.Response(200, json={"result": {}})

    sink = build_ticket_sink(
        _config(tmp_path),
        clock=FrozenClock(start=datetime(2026, 10, 21, tzinfo=UTC)),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ServiceNowError):
        sink.append_journal(SnTable.INCIDENT, "sys-1", JournalField.WORK_NOTES, "note")
    assert posts["n"] == 1  # the note may have landed: the executor reconciles instead
