"""A demo ServiceNow for a local install without an instance (``./install.sh``, demo mode).

It serves the Table API the real adapter calls (the contract suite's
``FakeServiceNowHTTP``), seeded so every ``doctor`` ServiceNow check passes:
the integration user with ``itil`` only, readable history tables, rate-limit
headers. A new sample incident on one of the demo catalog's services
(``nightly-sync``, ``core-db``, ``payroll-api``) arrives shortly after start
and then every ``--interval-s`` seconds, updated "now" by a person, so
the worker's next poll picks it up and triages it.

Nothing here is real: no tickets leave the container, and a restart starts
from an empty instance. ``supportagent demo-servicenow`` serves it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP
from supportagent.ports.clock import Clock

INTEGRATION_USER = "svc_supportagent"
_LONG_AGO = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class DemoIncident:
    short_description: str
    description: str
    priority: str
    cmdb_ci: str
    ci_name: str


#: Sample incidents on the demo catalog's CIs (``local/catalog.yaml`` in demo mode).
DEMO_INCIDENTS: tuple[DemoIncident, ...] = (
    DemoIncident(
        "core-db connection pool exhausted",
        "Every request fails with 'remaining connection slots are reserved'.",
        "2",
        "CI2002",
        "core-db",
    ),
    DemoIncident(
        "payroll-api returning HTTP 500 since the 14:00 deploy",
        "Error rate jumped from 0.1% to 12% right after the release went out.",
        "3",
        "CI3003",
        "payroll-api",
    ),
    DemoIncident(
        "nightly-sync DAG failed: upstream extract missing",
        "The 02:00 run failed at the load step; the upstream file was not delivered.",
        "3",
        "CI1001",
        "nightly-sync",
    ),
    DemoIncident(
        "core-db replication lag above 5 minutes",
        "Read replicas are serving stale balances; lag keeps growing.",
        "2",
        "CI2002",
        "core-db",
    ),
    DemoIncident(
        "payroll-api p99 latency over 2 seconds",
        "Slow responses on /payslips since this morning, no errors.",
        "3",
        "CI3003",
        "payroll-api",
    ),
)


def seed_healthy(sn: FakeServiceNowHTTP) -> None:
    """The instance shape every ``doctor`` ServiceNow check passes against."""
    sn.seed_row("sys_user", {"sys_id": "u-svc", "user_name": INTEGRATION_USER})
    sn.seed_row("sys_user", {"sys_id": "u-alice", "user_name": "alice"})
    sn.seed_row("sys_user_role", {"sys_id": "r-itil", "name": "itil"})
    sn.seed_row(
        "sys_user_has_role",
        {"sys_id": "uhr-1", "user": "u-svc", "role": "r-itil", "state": "active"},
    )
    sn.seed_row(
        "sys_security_acl",
        {"sys_id": "acl-wn", "name": "task.work_notes", "operation": "read", "active": "true"},
    )
    sn.seed_row(
        "sys_security_acl_role",
        {"sys_id": "ar-1", "sys_security_acl": "acl-wn", "sys_user_role": "r-itil"},
    )
    sn.seed_row(
        "sys_security_acl",
        {"sys_id": "acl-w", "name": "incident", "operation": "write", "active": "true"},
    )
    sn.seed_row(
        "sys_security_acl_role",
        {"sys_id": "ar-2", "sys_security_acl": "acl-w", "sys_user_role": "r-itil"},
    )
    sn.seed_row(
        "sys_auto_flush",
        {"sys_id": "af-1", "tablename": "sys_audit", "age": str(90 * 86_400), "active": "true"},
    )
    sn.set_response_headers({"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4900"})
    # One old, closed incident with a work note, so the history tables have rows
    # (doctor's history_access); it predates any sync window, so it isn't triaged.
    sn.seed_record(
        "incident",
        "demo-0000",
        number="INC0010000",
        short_description="Mail delivery delayed (resolved)",
        priority="4",
        state="7",
        cmdb_ci="CI3003",
        cmdb_ci_name="payroll-api",
        caller_id="u-alice",
        updated_at=_LONG_AGO,
        updated_by="alice",
        created_by="alice",
    )
    sn.seed_journal(
        "incident", "demo-0000", user="alice", ts=_LONG_AGO, element="work_notes", value="Fixed."
    )


def add_incident(sn: FakeServiceNowHTTP, n: int, *, now: datetime, run: str = "0001") -> str:
    """The ``n``-th sample incident (cycling), updated now by a person; returns its number.

    ``run`` tells demo processes apart: sync has seen a restarted demo's earlier
    ids, so each start numbers its incidents afresh (``INC<run><n>``).
    """
    sample = DEMO_INCIDENTS[n % len(DEMO_INCIDENTS)]
    number = f"INC{run}{n + 1:03d}"
    sys_id = f"demo-{run}-{n + 1:04d}"
    sn.seed_record(
        "incident",
        sys_id,
        number=number,
        short_description=sample.short_description,
        description=sample.description,
        priority=sample.priority,
        state="1",
        cmdb_ci=sample.cmdb_ci,
        cmdb_ci_name=sample.ci_name,
        caller_id="u-alice",
        updated_at=now,
        updated_by="alice",
        created_by="alice",
    )
    sn.seed_journal("incident", sys_id, user="alice", ts=now, element="work_notes", value="Opened.")
    return number


def build(
    *, clock: Clock, interval_s: float, first_after_s: float
) -> tuple[FakeServiceNowHTTP, threading.Event]:
    """The seeded instance and a stop flag for its incident feed."""
    sn = FakeServiceNowHTTP()
    seed_healthy(sn)

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"service": "demo-servicenow", "status": "ok"})

    sn.app.router.routes.append(Route("/health", health, methods=["GET"]))
    stop = threading.Event()

    run = f"{int(clock.now().timestamp()) // 60 % 10_000:04d}"

    def feed() -> None:
        n = 0
        wait = first_after_s
        while not stop.wait(wait):
            number = add_incident(sn, n, now=clock.now(), run=run)
            print(f"demo-servicenow: new incident {number}", flush=True)
            n += 1
            wait = interval_s

    threading.Thread(target=feed, name="demo-incidents", daemon=True).start()
    return sn, stop
