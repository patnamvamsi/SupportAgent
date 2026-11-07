"""Generate the evidence captured during System A's past incidents (WP-O6, ADR-0015 §5).

    uv run python evals/onboarding/generate_evidence.py

Readiness replays a system's history as lab scenarios. Each scenario's
*world* is what the read-only tools returned for that incident's window:
here, ``system-a/sources/evidence/<number>.yaml``, one per incident whose
evidence was kept. It is written from each incident's own record, so it
says what the resolver's notes say: a deadlock note leaves ORA-00060 lines
and no blocking chain; a bad deploy's rollout is its change record's;
a gateway ticket shows the gateway's logs, not its dependency's.

Some incidents have no evidence, as in any real history. Generation is
seeded per incident and reads only the generated history, so it never
changes ``generate_history.py``'s output.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parent
SYSTEM = ROOT / "system-a"
#: The share of incidents whose evidence wasn't kept.
UNCAPTURED = 0.12
#: Each service's database and namespace, as the system graph has them.
DATABASE = {
    "payment-service": "paydb",
    "orders-service": "paydb",
    "settlement-batch": "paydb",
    "customer-service": "crmdb",
}
NAMESPACE, HIKARI = (
    "payments",
    "HikariPool-1 - Connection is not available, request timed out after 30000ms.",
)

World = dict[str, Any]


def _ts(at: datetime, minutes: int) -> str:
    return (at - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def logs(at: datetime, service: str, lines: list[str]) -> dict[str, Any]:
    matches = [
        {"ts": _ts(at, len(lines) - i), "line": line, "labels": {"service": service}}
        for i, line in enumerate(lines)
    ]
    return {
        "result": {
            "query": f'{{service="{service}"}}',
            "window_min": 60,
            "count": len(lines),
            "matches": matches,
        }
    }


def metric(value: float) -> dict[str, Any]:
    return {
        "result": {
            "query": "q",
            "series": [{"labels": {}, "value": value}],
            "count": 1,
            "max_value": value,
        }
    }


def locks(db: str, chain: list[tuple[int, int, float]]) -> dict[str, Any]:
    blockers = [
        {
            "sid": sid,
            "blocking": n,
            "status": "INACTIVE",
            "sql_id": None,
            "event": "enq: TX - row lock contention",
            "wait_s": wait,
        }
        for sid, n, wait in chain
    ]
    return {
        "result": {"database": db, "blocked": sum(n for _, n, _ in chain), "blockers": blockers}
    }


def pods(
    service: str,
    n: int,
    *,
    restarts: int = 0,
    termination: str | None = None,
    namespace: str = NAMESPACE,
) -> dict[str, Any]:
    items = [
        {
            "name": f"{service}-{i}",
            "phase": "Running",
            "ready": True,
            "restarts": restarts,
            "last_termination": termination,
            "reason": None,
            "waiting": None,
        }
        for i in range(n)
    ]
    return {
        "result": {
            "namespace": namespace,
            "selector": f"app={service}",
            "pods": items,
            "restarts_total": restarts * n,
            "oomkilled": n if termination == "OOMKilled" else 0,
            "not_ready": 0,
        }
    }


def lock_contention(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    db, svc, notes = DATABASE[inc["cmdb_ci"]], inc["cmdb_ci"], inc["close_notes"]
    if "ORA-00060" in notes or "Deadlock" in notes:  # cleared itself: only the log lines remain
        lines = ["ORA-00060: deadlock detected while waiting for resource"] * rnd.randint(3, 6)
        return {
            "oracle.locks": locks(db, []),
            "logs.search": logs(at, svc, lines + [HIKARI] * rnd.randint(0, 2)),
        }
    sid = int(m.group(0)) if (m := re.search(r"\b\d{3,4}\b", notes)) else rnd.randint(100, 3000)
    blocked = rnd.randint(10, 40)
    return {
        "oracle.locks": locks(db, [(sid, blocked, float(rnd.randint(300, 1500)))]),
        "logs.search": logs(at, svc, [HIKARI] * rnd.randint(2, 6)),  # the pool starves behind it
        "metrics.query": metric(round(rnd.uniform(0.4, 0.8), 2)),
    }


def heap(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    svc = inc["cmdb_ci"]
    lines = rnd.sample(
        [
            "java.lang.OutOfMemoryError: Java heap space",
            'Exception in thread "export-1" java.lang.OutOfMemoryError: Java heap space',
            "java.lang.OutOfMemoryError: GC overhead limit exceeded",
        ],
        k=rnd.randint(1, 3),
    )
    killed = "OOMKilled" if "OOMKilled" in inc["close_notes"] else "Error"
    return {
        "logs.search": logs(at, svc, lines),
        "metrics.query": metric(round(rnd.uniform(0.2, 0.45), 2)),
        "k8s.pod_status": pods(svc, 3, restarts=rnd.randint(2, 9), termination=killed),
    }


def pool(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    svc = inc["cmdb_ci"]
    if svc == "payments-gateway":  # the gateway sees its dependency fail, not the pool behind it
        lines = ["Route payment-service returned 503 SERVICE_UNAVAILABLE"] * rnd.randint(4, 9)
        return {
            "logs.search": logs(at, svc, lines),
            "metrics.query": metric(round(rnd.uniform(0.3, 0.6), 2)),
        }
    db = DATABASE[svc]
    held = (
        "lock wait" in inc["close_notes"]
    )  # connections held during a lock wait: a short chain shows
    return {
        "logs.search": logs(at, svc, [HIKARI] * rnd.randint(4, 8)),
        "oracle.locks": locks(
            db,
            [(rnd.randint(100, 3000), rnd.randint(3, 8), float(rnd.randint(70, 200)))]
            if held
            else [],
        ),
        "metrics.query": metric(round(rnd.uniform(0.5, 0.9), 2)),
    }


def tablespace(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    svc, db = inc["cmdb_ci"], DATABASE[inc["cmdb_ci"]]
    spaces = [
        ("SETTLEMENT_DATA", 100.0),
        ("PAYMENTS_DATA", round(rnd.uniform(55, 80), 2)),
        ("USERS", 11.4),
    ]
    return {
        "oracle.tablespaces": {
            "result": {
                "database": db,
                "tablespaces": [{"name": n, "used_pct": p} for n, p in spaces],
                "max_used_pct": 100.0,
            }
        },
        "logs.search": logs(
            at,
            svc,
            [
                "ORA-01653: unable to extend table SETTLEMENT.SETTLEMENT_LINES by 8192 "
                "in tablespace SETTLEMENT_DATA"
            ],
        ),
        "oracle.locks": locks(db, []),
    }


def deploy(
    inc: dict[str, Any],
    at: datetime,
    rnd: random.Random,
    changes: dict[str, Any],
    namespace: str = NAMESPACE,
) -> World:
    svc = inc["cmdb_ci"]
    change = changes[inc["caused_by"]]
    started = datetime.fromisoformat(change["start_date"]).replace(tzinfo=UTC)
    minutes = max(1, int((at - started).total_seconds() // 60))
    rollout = {
        "source": "kubernetes",
        "kind": "rollout",
        "ref": f"{svc}-{rnd.randrange(16**6):06x}",
        "at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "minutes_ago": minutes,
        "actor": "deploy-bot",
        "environment": "prod",
    }
    return {
        "changes.recent": {"result": {"window_min": 180, "changes": [rollout], "count": 1}},
        "k8s.pod_status": pods(svc, 3, namespace=namespace),
    }


def upstream(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    svc = inc["cmdb_ci"]
    url = "https://api.cardco.example/v1/authorisations"
    line = f'I/O error on POST request for "{url}": Read timed out'
    return {
        "logs.search": logs(at, svc, [line] * rnd.randint(4, 9)),
        "metrics.query": metric(round(rnd.uniform(0.2, 0.5), 2)),
    }


def plan_regression(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    svc, db = inc["cmdb_ci"], DATABASE[inc["cmdb_ci"]]
    found = re.search(r"\b(?=[0-9a-z]*\d)[0-9a-z]{8}\b", inc["close_notes"])  # the SQL_ID
    sql_id = found.group(0) if found else f"{rnd.randrange(16**8):08x}"
    previous = round(rnd.uniform(80, 250), 1)
    slow = round(previous * rnd.uniform(15, 45), 1)
    statements = [
        {
            "sql_id": sql_id,
            "plans": 2,
            "mean_ms": slow,
            "previous_mean_ms": previous,
            "plan_changed": True,
            "slowdown": round(slow / previous, 3),
        },
        {
            "sql_id": f"{rnd.randrange(16**8):08x}",
            "plans": 1,
            "mean_ms": round(rnd.uniform(5, 60), 1),
            "previous_mean_ms": None,
            "plan_changed": False,
            "slowdown": 1.0,
        },
    ]
    return {
        "oracle.top_sql": {
            "result": {
                "database": db,
                "statements": statements,
                "max_slowdown": statements[0]["slowdown"],
            }
        },
        "oracle.locks": locks(db, []),
        "logs.search": logs(at, svc, []),
    }


BUILDERS: dict[str, Callable[..., World]] = {
    "oracle-lock-contention": lock_contention,
    "jvm-heap-exhaustion": heap,
    "connection-pool-exhaustion": pool,
    "oracle-tablespace-full": tablespace,
    "bad-deploy": deploy,
    "card-processor-outage": upstream,
    "oracle-plan-regression": plan_regression,
    # portal-cors-or-bundle: a browser-side fault; no read-only tool saw it.
    # noise: access requests leave no evidence.
}


# --- System C (ADR-0016): Node.js on MongoDB and RabbitMQ ---------------------------

C_NAMESPACE = "logistics"
#: The share of backlogs whose consumers were gone (the rest: too few for a burst).
STALLED = 0.6
C_DATABASE = {"shipments-api": "shipmentsdb", "tracking-worker": "trackingdb"}
LOOP = 'max(nodejs_eventloop_lag_p99_seconds{service="%s"})'


def slow_ops(db: str, ops: list[tuple[str, str, float, str]]) -> dict[str, Any]:
    return {
        "result": {
            "database": db,
            "ops": [{"ns": n, "op": o, "ms": ms, "plan": p} for n, o, ms, p in ops],
            "max_ms": max((o[2] for o in ops), default=0.0),
            "collscans": sum(1 for o in ops if o[3] == "COLLSCAN"),
        }
    }


def queues(qs: list[tuple[str, int, int, float, float]], alarms: list[str]) -> dict[str, Any]:
    rows = [
        {"name": n, "messages": m, "consumers": c, "publish_rate": p, "deliver_rate": d}
        for n, m, c, p, d in qs
    ]
    return {
        "result": {
            "broker": "logistics-mq",
            "queues": rows,
            "max_messages": max((r["messages"] for r in rows), default=0),
            "alarms": alarms,
        }
    }


def c_missing_index(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    svc = inc["cmdb_ci"]
    db = C_DATABASE[svc]
    scans = [(f"{db}.shipments", "find", float(rnd.randint(4000, 12000)), "COLLSCAN")]
    scans += [(f"{db}.shipments", "find", float(rnd.randint(30, 90)), "IXSCAN")]
    return {"mongodb.slow_ops": slow_ops(db, scans), "logs.search": logs(at, svc, [])}


def c_pool(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    svc = inc["cmdb_ci"]
    line = (
        "MongoWaitQueueTimeoutError: Timed out while checking out a connection from connection pool"
    )
    return {
        "mongodb.slow_ops": slow_ops(
            C_DATABASE[svc], [(f"{C_DATABASE[svc]}.shipments", "find", 60.0, "IXSCAN")]
        ),
        "logs.search": logs(at, svc, [line] * rnd.randint(4, 8)),
    }


def c_backlog(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    depth = rnd.randint(120, 410) * 1000
    stalled = rnd.random() < STALLED  # crashed consumers, or too few for a burst
    consumers, deliver = (0, 0.0) if stalled else (2, 20.0)
    return {
        "rabbitmq.queues": queues(
            [
                ("shipment-events", depth, consumers, 60.0, deliver),
                ("carrier-scans", 300, 2, 5.0, 5.0),
            ],
            [],
        )
    }


def c_memory_alarm(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    return {
        "rabbitmq.queues": queues(
            [("shipment-events", rnd.randint(500, 3000), 3, 0.0, 0.0)], ["memory"]
        )
    }


def c_event_loop(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    svc = inc["cmdb_ci"]
    lines = ["Error: connect ETIMEDOUT 10.8.1.20:443"] * rnd.randint(3, 6)
    return {
        "metrics.query": _loop(svc, round(rnd.uniform(2.0, 5.0), 2)),
        "logs.search": logs(at, svc, lines),
    }


def _loop(svc: str, value: float) -> dict[str, Any]:
    return {
        "result": {
            "query": LOOP % svc,
            "series": [{"labels": {}, "value": value}],
            "count": 1,
            "max_value": value,
        }
    }


def c_heap(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    svc = inc["cmdb_ci"]
    lines = [
        "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
        "<--- Last few GCs --->",
    ]
    return {
        "logs.search": logs(at, svc, lines),
        "k8s.pod_status": pods(
            svc, 4, restarts=rnd.randint(3, 9), termination="Error", namespace=C_NAMESPACE
        ),
    }


def c_upstream(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    svc = inc["cmdb_ci"]
    line = "Request failed with status code 503 (api.fastship.example)"
    return {
        "logs.search": logs(at, svc, [line] * rnd.randint(4, 9)),
        "metrics.query": _loop(svc, 0.03),
    }


def c_deploy(
    inc: dict[str, Any], at: datetime, rnd: random.Random, changes: dict[str, Any]
) -> World:
    return deploy(inc, at, rnd, changes, namespace=C_NAMESPACE)


BUILDERS_C: dict[str, Callable[..., World]] = {
    "mongo-missing-index": c_missing_index,
    "tracking-backlog": c_backlog,
    "event-loop-blocked": c_event_loop,
    "node-heap-oom": c_heap,
    "bad-deploy": c_deploy,
    "carrier-outage": c_upstream,
    "mongo-pool": c_pool,
    "rabbit-memory-alarm": c_memory_alarm,
    # noise: access requests leave no evidence.
}


# --- System B: an analytics platform on Kafka, Snowflake, Spark and Streamlit ----------------

B_NAMESPACE = "analytics-apps"


def b_lag(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    lag = rnd.randint(30, 250) * 10_000  # 300k-2.5M messages behind, still growing
    groups = [
        {"group": "stream-ingest", "topic": "events.clickstream", "lag": lag, "trend": "rising"}
    ]
    return {"kafka.consumer_lag": {"result": {"cluster": "analytics-msk", "groups": groups}}}


def b_late_upstream(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    notes = inc.get("close_notes", "")
    upstream = "orders-cdc-ingest" if "orders-cdc-ingest" in notes else "snowflake-sink-orders"
    late = float(rnd.choice([24, 30, 48]))
    return {
        "data.freshness": {
            "result": {
                "datasets": [
                    {
                        "name": "ANALYTICS.DAILY_SALES",
                        "hours_late": late,
                        "upstream": upstream,
                        "upstream_state": "FAILED",
                    },
                ]
            }
        },
        # the build ran on time, on an empty RAW.ORDERS: it didn't fail
        "snowflake.task_history": {
            "result": {
                "tasks": [
                    {"name": "ANALYTICS.TASK_BUILD_DAILY_SALES", "state": "SUCCEEDED", "error": ""}
                ]
            }
        },
    }


def b_executor_oom(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    reason = (
        "Container killed by YARN for exceeding memory limits. "
        "12.6 GB of 12 GB physical memory used"
        if "YARN" in inc["description"]
        else "ExecutorLostFailure (executor exited caused by one of the running tasks): "
        "java.lang.OutOfMemoryError"
    )
    losses = [
        {"executor": str(rnd.randint(2, 20)), "reason": reason} for _ in range(rnd.randint(1, 3))
    ]
    app = {
        "name": f"batch-features-{at:%Y-%m-%d}",
        "state": "FAILED",
        "failure": "Job aborted due to stage failure",
        "executor_losses": losses,
    }
    return {"spark.applications": {"result": {"apps": [app]}}}


def b_queuing(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    queued = round(rnd.uniform(1.5, 4.0), 1)
    return {
        "snowflake.warehouse_load": {
            "result": {
                "warehouses": [
                    {
                        "name": "WH_BI",
                        "avg_running": round(rnd.uniform(6, 9), 1),
                        "avg_queued_load": queued,
                        "state": "STARTED",
                    }
                ]
            }
        },
        "snowflake.resource_monitors": {
            "result": {
                "monitors": [
                    {
                        "name": "RM_ANALYTICS",
                        "used_pct": float(rnd.randint(40, 85)),
                        "suspended": [],
                    }
                ]
            }
        },
    }


def b_task_failure(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    text = inc["description"]
    error = (
        "SQL compilation error: invalid identifier 'DISCOUNT_AMT'"
        if "DISCOUNT_AMT" in text
        else "Number of columns in insert does not match columns in table"
    )
    return {
        "snowflake.task_history": {
            "result": {
                "tasks": [
                    {"name": "ANALYTICS.TASK_BUILD_DAILY_SALES", "state": "FAILED", "error": error}
                ]
            }
        },
        # the table is stale because the build failed; its upstream delivered
        "data.freshness": {
            "result": {
                "datasets": [
                    {
                        "name": "ANALYTICS.DAILY_SALES",
                        "hours_late": 26.0,
                        "upstream": "snowflake-sink-orders",
                        "upstream_state": "RUNNING",
                    }
                ]
            }
        },
    }


def b_schema_drift(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    notes = inc.get("close_notes", "")
    subject = (
        "cdc.customers-value"
        if "cdc.customers" in inc["short_description"]
        else "events.orders-value"
    )
    change = "removed a field" if "removed" in notes else "added a required field"
    version = rnd.randint(8, 30)
    changes = [
        {"subject": subject, "version": version, "compatible": False, "change": change},
        {
            "subject": subject,
            "version": version - 1,
            "compatible": True,
            "change": "added an optional field",
        },
    ]
    return {"data.schema_changes": {"result": {"changes": changes}}}


def b_app_crash(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    return {
        "k8s.pod_status": pods(
            inc["cmdb_ci"],
            2,
            restarts=rnd.randint(3, 8),
            termination="OOMKilled",
            namespace=B_NAMESPACE,
        )
    }


def b_credit_quota(inc: dict[str, Any], at: datetime, rnd: random.Random) -> World:
    return {
        "snowflake.resource_monitors": {
            "result": {
                "monitors": [
                    {"name": "RM_ANALYTICS", "used_pct": 100.0, "suspended": ["WH_TRANSFORM"]}
                ]
            }
        },
        # suspended: it runs nothing, so nothing queues on it
        "snowflake.warehouse_load": {
            "result": {
                "warehouses": [
                    {
                        "name": "WH_TRANSFORM",
                        "avg_running": 0.0,
                        "avg_queued_load": 0.0,
                        "state": "SUSPENDED",
                    }
                ]
            }
        },
    }


BUILDERS_B: dict[str, Callable[..., World]] = {
    "stream-consumer-lag": b_lag,
    "late-upstream-data": b_late_upstream,
    "spark-executor-oom": b_executor_oom,
    "warehouse-queuing": b_queuing,
    "snowflake-task-failure": b_task_failure,
    "cdc-schema-drift": b_schema_drift,
    "streamlit-app-crash": b_app_crash,
    "credit-quota": b_credit_quota,
    # noise: access requests leave no evidence.
}


BY_SYSTEM = {"system-a": BUILDERS, "system-b": BUILDERS_B, "system-c": BUILDERS_C}
_NEED_CHANGES = (deploy, c_deploy)


def generate(system: Path = SYSTEM, out: Path | None = None) -> int:
    """Write ``<out>`` (default ``<system>/sources/evidence``) from the system's history."""
    sn = system / "sources" / "servicenow"
    incidents = {i["number"]: i for i in yaml.safe_load((sn / "incidents.yaml").read_text())}
    changes = {c["number"]: c for c in yaml.safe_load((sn / "changes.yaml").read_text())}
    truth = yaml.safe_load((system / "truth_incidents.yaml").read_text())["incident_classes"]
    out = out if out is not None else system / "sources" / "evidence"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.yaml"):
        old.unlink()
    header = "# Generated by evals/onboarding/generate_evidence.py: edit the builders there.\n"
    kept = 0
    for cls in truth:
        build = BY_SYSTEM.get(system.name, {}).get(cls["id"])
        for number in cls["incidents"] if build else ():
            inc = incidents[number]
            rnd = random.Random(f"evidence-{number}")  # noqa: S311 — fixtures, not secrets
            if rnd.random() < UNCAPTURED:
                continue
            at = datetime.fromisoformat(inc["opened_at"]).replace(tzinfo=UTC)
            world = build(inc, at, rnd, changes) if build in _NEED_CHANGES else build(inc, at, rnd)
            body = {"incident": number, "captured_at": inc["opened_at"], "world": world}
            (out / f"{number}.yaml").write_text(
                header + yaml.safe_dump(body, sort_keys=False, width=110)
            )
            kept += 1
    return kept


if __name__ == "__main__":
    for name in BY_SYSTEM:
        print(f"{name}: evidence for {generate(ROOT / name)} incidents")
