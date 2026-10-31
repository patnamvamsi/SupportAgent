"""Generate the reference systems' ServiceNow history and its truth (WP-O7, ADR-0015).

    uv run python evals/onboarding/generate_history.py

Each incident class below becomes ``count`` incidents with varied wording on
the class's CIs, resolution notes that reveal the cause the way a real
resolver writes them, a problem record for the recurring ones, and, for
deploy-caused classes, the change that caused them. The same pass writes
``truth_incidents.yaml``: which incidents form each class, its taxonomy labels
and its cause. Generation is seeded, so the committed files are reproducible;
edit a class here and re-run, never the YAML by hand.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
START = datetime(2025, 11, 1, tzinfo=UTC)
DAYS = 345  # to mid-October 2026


@dataclass
class IncidentClass:
    id: str
    labels: list[str]  # the preferred taxonomy label first, then acceptable ones
    cause: str | None  # the hypothesis id a good onboarding maps it to; None for noise
    count: int
    cis: list[str]
    priorities: list[str]
    subjects: list[str]
    bodies: list[str]
    notes: list[str]
    category: str
    problem: str | None = None  # a problem record title for a recurring class
    change: str | None = None  # the change that caused it (deploy classes)
    words: dict[str, list[str]] = field(default_factory=dict)


SYSTEM_A = [
    IncidentClass(
        "oracle-lock-contention",
        # Latency is acceptable too: the incident lab's pg-lock-contention is database/latency.
        ["database/saturation", "database/latency"],
        "oracle/lock_contention",
        7,
        ["payment-service"],
        ["2", "2", "3"],
        [
            "Card captures timing out",
            "payment-service capture latency very high",
            "Captures failing with ORA-00060",
            "Payments stuck in CAPTURING",
        ],
        [
            "Capture API p99 above 20s since {t}; many requests time out at the gateway.",
            "payment-service logs show ORA-00060: deadlock detected while waiting for resource.",
            "Ops report payments stuck in CAPTURING; capture retries piling up.",
        ],
        [
            "DBA found blocking session {sid} holding row locks on PAYMENTS.CAPTURES (enq: TX - row lock contention); killed it. Captures recovered.",
            "Deadlock between two capture retries updating the same rows (ORA-00060). DBA killed the blocker; retry backoff to be fixed under the problem record.",
            "V$SESSION showed a long blocking chain on PAYMENTS; blocker was a stuck capture retry. Session killed by DBA.",
        ],
        "Database",
        problem="Recurring row-lock contention in PAYMENTS capture retries",
        words={"sid": ["2231", "877", "1490", "3302"]},
    ),
    IncidentClass(
        "jvm-heap-exhaustion",
        ["app/memory"],
        "jvm/heap_exhaustion",
        6,
        ["orders-service"],
        ["2", "3"],
        [
            "orders-service pods restarting",
            "OutOfMemoryError in orders-service",
            "Order export failing",
            "orders-service GC overhead limit exceeded",
        ],
        [
            "Pods in CrashLoopBackOff with java.lang.OutOfMemoryError: Java heap space.",
            "Order export endpoint returns 500; logs show GC overhead limit exceeded.",
            "Heap usage climbing to the -Xmx limit before each restart.",
        ],
        [
            "Heap exhausted by the order export loading a full result set. Restarted pods; export limited to 10k rows.",
            "OOMKilled after the monthly export; heap dump shows OrderExportDto list. Pods restarted.",
            "java.lang.OutOfMemoryError: Java heap space during export. Raised -Xmx temporarily; fix tracked in problem.",
        ],
        "Software",
        problem="orders-service export loads unbounded result sets into heap",
    ),
    IncidentClass(
        "connection-pool-exhaustion",
        ["database/saturation"],
        "jvm/connection_pool_exhaustion",
        6,
        ["payments-gateway", "payment-service"],
        ["2", "3"],
        [
            "payments-gateway returning 503",
            "Checkout failing intermittently",
            "HikariPool connection timeouts",
            "API errors across payments",
        ],
        [
            "Gateway 503s; payment-service logs: HikariPool-1 - Connection is not available, request timed out after 30000ms.",
            "Intermittent checkout failures; Hikari pending connections at 40/40.",
            "All payment APIs slow, then 503 from the gateway.",
        ],
        [
            "Hikari pool exhausted: a slow report query held connections. Query killed; pool recovered.",
            "Connection pool exhausted (40/40 active, 200 pending) behind a long-running query; recovered after it finished.",
            "HikariPool timeouts caused by connections held during a lock wait. Recovered after the blocker cleared.",
        ],
        "Database",
        problem="payment-service connection pool exhausted by long-held connections",
    ),
    IncidentClass(
        "oracle-tablespace-full",
        ["database/failure", "batch/failure"],
        "oracle/tablespace_full",
        4,
        ["settlement-batch"],
        ["2", "3"],
        [
            "Settlement batch failed",
            "Nightly settlement did not run to completion",
            "Settlement file not sent to bank",
        ],
        [
            "settlement-batch failed at the write step with ORA-01653: unable to extend table SETTLEMENT.SETTLEMENT_LINES.",
            "No SETTLE file at the bank this morning; batch job status FAILED.",
        ],
        [
            "SETTLEMENT tablespace full (ORA-01653). DBA added a datafile; batch re-run at 06:10.",
            "Tablespace SETTLEMENT_DATA at 100%. Datafile added, settlement re-run and file sent.",
        ],
        "Database",
        problem="SETTLEMENT tablespace has no autoextend",
    ),
    IncidentClass(
        "bad-deploy",
        ["deploy/failure", "deploy/rollback"],
        "recent_change_push",
        6,
        ["orders-service", "payment-service", "payments-gateway"],
        ["2", "3"],
        [
            "5xx spike after release",
            "Errors after the {svc} deploy",
            "Orders API failing since deploy",
        ],
        [
            "5xx rate jumped from 0.2% to 9% minutes after the {svc} release.",
            "New version of {svc} throws NullPointerException on /api/orders.",
        ],
        [
            "Caused by the {svc} release; rolled back with helm rollback, errors stopped.",
            "Regression in the latest {svc} build. Rolled back; fix in next release.",
        ],
        "Software",
        change="Deploy {svc} release",
        words={"svc": ["orders-service", "payment-service", "payments-gateway"]},
    ),
    IncidentClass(
        "card-processor-outage",
        ["network/outage"],
        "upstream_provider",
        5,
        ["payment-service"],
        ["1", "2"],
        ["Card authorisations failing", "CardCo timeouts", "Payments declined en masse"],
        [
            "payment-service: read timeout after 4000ms calling api.cardco.example.",
            "Authorisation success rate dropped to 12%; CardCo status page shows degraded.",
        ],
        [
            "CardCo incident on their side (status page). Recovered when CardCo resolved it; no change on our side.",
            "Upstream provider CardCo outage. Waited it out; added a status-page check to the runbook.",
        ],
        "Network",
    ),
    IncidentClass(
        "portal-cors-or-bundle",
        ["app/config"],
        "ui/cors_or_stale_bundle",
        4,
        ["payments-portal"],
        ["3", "4"],
        [
            "Payments portal shows a blank page",
            "Portal cannot load data",
            "Back-office UI broken after release",
        ],
        [
            "Browser console: blocked by CORS policy: No 'Access-Control-Allow-Origin' header.",
            "Blank page; console shows 404 for main.{h}.js.",
        ],
        [
            "CORS: gateway globalcors no longer allowed the portal origin after a config change. Restored allowedOrigins.",
            "Stale bundle in CDN cache referencing old main.{h}.js. Purged the CDN cache.",
        ],
        "Software",
        words={"h": ["a81f2c", "9d03be", "41c7aa"]},
    ),
    IncidentClass(
        "oracle-plan-regression",
        ["database/latency"],
        "oracle/plan_regression",
        4,
        ["orders-service", "customer-service"],
        ["3"],
        ["Order search slow", "Customer lookup taking seconds", "Latency on {svc} queries"],
        [
            "p99 of order search up from 200ms to 6s; nothing deployed.",
            "Customer lookup slow; DB time dominated by one SQL_ID.",
        ],
        [
            "Execution plan changed after stats gathering (SQL_ID {sqlid} full scan). DBA pinned the old plan with a SQL plan baseline.",
            "Plan regression on {sqlid}; baseline restored, latency back to normal.",
        ],
        "Database",
        words={
            "sqlid": ["7h2kd9x1", "a1b2c3d4", "9zq8w7e6"],
            "svc": ["orders-service", "customer-service"],
        },
    ),
    IncidentClass(
        "noise",
        ["request/access"],
        None,
        3,
        ["payments-portal"],
        ["4"],
        [
            "Access to payments portal for new starter",
            "Password reset for portal user",
            "Add user to PAY-L2 group",
        ],
        ["Please grant access.", "User locked out."],
        ["Access granted.", "Password reset.", "Done."],
        "Inquiry / Help",
    ),
]

SYSTEM_B = [
    IncidentClass(
        "stream-consumer-lag",
        ["stream/lag", "queue/backlog"],
        "kafka/consumer_lag",
        7,
        ["stream-ingest"],
        ["2", "3"],
        [
            "Clickstream data delayed",
            "Kafka consumer lag growing on events.clickstream",
            "Real-time dashboard behind by hours",
        ],
        [
            "Consumer group stream-ingest lag on events.clickstream above {n} messages and rising.",
            "Clickstream in RAW.CLICKSTREAM is {m} minutes behind.",
        ],
        [
            "Spark streaming micro-batches slowed (executors lost); consumer group lag grew. Scaled executors, lag drained.",
            "Lag caused by a partition rebalance storm after a broker restart; consumers caught up after rebalancing settled.",
            "stream-ingest checkpoint corruption stalled the query; restarted from the last good checkpoint, lag drained.",
        ],
        "Data",
        problem="stream-ingest falls behind when executors are lost",
        words={"n": ["2M", "5M", "800k"], "m": ["90", "240", "45"]},
    ),
    IncidentClass(
        "late-upstream-data",
        ["data/freshness", "batch/schedule"],
        "data/late_upstream",
        6,
        ["daily-sales-task", "customer-insights"],
        ["2", "3"],
        [
            "Daily sales dashboard not updated",
            "ANALYTICS.DAILY_SALES stale",
            "Yesterday's sales missing in Streamlit",
        ],
        [
            "DAILY_SALES last loaded {d}; the Streamlit app shows yesterday as zero.",
            "Business reports missing yesterday's orders.",
        ],
        [
            "Upstream RAW.ORDERS was empty: the Kafka Connect Snowflake sink for events.orders had failed. Restarted the connector, backfilled, re-ran the task.",
            "Late upstream data: orders-cdc-ingest connector stopped on a task error; after restart, DAILY_SALES rebuilt.",
        ],
        "Data",
        problem="Snowflake sink connector failures are not alerted",
        words={"d": ["2 days ago", "yesterday 02:00"]},
    ),
    IncidentClass(
        "spark-executor-oom",
        ["job/failure", "batch/failure"],
        "spark/executor_oom",
        6,
        ["batch-features"],
        ["3"],
        [
            "customer_features DAG failed",
            "batch-features Spark job failed",
            "Feature table not refreshed",
        ],
        [
            "Airflow task run_batch_features failed; Spark: ExecutorLostFailure (executor 7 exited caused by OOM).",
            "Job failed after 3 retries: Container killed by YARN for exceeding memory limits.",
        ],
        [
            "Executor OOM from data skew on customer_id; salted the join and re-ran.",
            "Spark executors exceeded memory on the join stage (skewed key). Increased executor memory, re-ran.",
        ],
        "Data",
        problem="batch-features join skew causes executor OOM",
    ),
    IncidentClass(
        "warehouse-queuing",
        ["warehouse/queuing", "request/capacity"],
        "snowflake/warehouse_queuing",
        5,
        ["customer-insights"],
        ["3"],
        [
            "Streamlit app very slow",
            "Customer insights pages time out",
            "Dashboards loading for minutes",
        ],
        [
            "Pages take 2-3 minutes; queries show QUEUED in query history on WH_BI.",
            "Users report time-outs at 09:00 Monday.",
        ],
        [
            "WH_BI saturated (queued load high at month start). Raised max cluster count to 3.",
            "Warehouse queuing on WH_BI from concurrent dashboard refreshes; scaled the warehouse.",
        ],
        "Data",
    ),
    IncidentClass(
        "snowflake-task-failure",
        ["job/failure", "batch/failure"],
        "snowflake/task_failure",
        5,
        ["daily-sales-task"],
        ["2", "3"],
        [
            "TASK_BUILD_DAILY_SALES failed",
            "Snowflake task error in ANALYTICS",
            "Daily sales build failed",
        ],
        [
            "TASK_HISTORY shows FAILED: SQL compilation error: invalid identifier 'DISCOUNT_AMT'.",
            "Task failed with Number of columns in insert does not match.",
        ],
        [
            "Task failed because an upstream column was renamed (schema drift in RAW.ORDERS). Updated the task SQL.",
            "TASK_BUILD_DAILY_SALES SQL error after RAW.ORDERS change; fixed SQL and resumed the task.",
        ],
        "Data",
    ),
    IncidentClass(
        "cdc-schema-drift",
        ["data/schema_drift", "data/correctness"],
        "data/schema_drift",
        4,
        ["orders-cdc-ingest"],
        ["2", "3"],
        [
            "Orders ingest rejecting records",
            "cdc.customers messages failing deserialisation",
            "Kafka Connect errors on schema",
        ],
        [
            "Connector logs: Schema being registered is incompatible with an earlier schema.",
            "Dead-letter topic filling with orders events.",
        ],
        [
            "Producer team changed the Avro schema (removed a field) without compatibility; schema registry rejected it. Reverted the producer schema.",
            "Schema drift on events.orders: new required field. Producer rolled back; DLQ replayed.",
        ],
        "Data",
    ),
    IncidentClass(
        "streamlit-app-crash",
        ["app/memory", "deploy/failure"],
        "streamlit/app_crash",
        4,
        ["customer-insights"],
        ["3"],
        ["customer-insights app down", "Streamlit pod crashlooping", "Insights app returns 502"],
        [
            "Pod customer-insights in CrashLoopBackOff, OOMKilled.",
            "502 from the ingress; app container restarting.",
        ],
        [
            "Streamlit app loaded a whole table into a pandas DataFrame; OOMKilled. Added caching with limits and pagination.",
            "OOMKilled after a new page loaded ANALYTICS.CUSTOMER_FEATURES fully; reverted the page.",
        ],
        "Software",
    ),
    IncidentClass(
        "credit-quota",
        ["warehouse/credit_quota", "request/capacity"],
        "snowflake/credit_quota",
        3,
        ["daily-sales-task", "customer-insights"],
        ["2"],
        [
            "Snowflake warehouse suspended",
            "All queries failing on WH_TRANSFORM",
            "Resource monitor suspended warehouse",
        ],
        [
            "Queries fail: Warehouse WH_TRANSFORM cannot be resumed because resource monitor has exceeded its quota."
        ],
        [
            "Monthly credit quota reached on RM_ANALYTICS; finance approved a quota increase, warehouse resumed."
        ],
        "Data",
    ),
    IncidentClass(
        "noise",
        ["request/access"],
        None,
        2,
        ["customer-insights"],
        ["4"],
        ["Access to customer insights app", "New Snowflake reader role for analyst"],
        ["Please grant access."],
        ["Access granted.", "Role granted by the platform team."],
        "Inquiry / Help",
    ),
]


def _fill(text: str, rnd: random.Random, cls: IncidentClass, svc: str) -> str:
    values = {k: rnd.choice(v) for k, v in cls.words.items()}
    values.setdefault("svc", svc)
    values.setdefault("t", f"{rnd.randint(0, 23):02d}:{rnd.choice(['05', '20', '40'])}")
    return text.format(**values)


def generate(
    system: str, classes: list[IncidentClass], prefix: str, seed: int, root: Path = ROOT
) -> None:
    rnd = random.Random(seed)
    incidents, problems, changes, truth = [], [], [], []
    n = 0
    slots = sorted(rnd.sample(range(DAYS * 24), sum(c.count for c in classes)))
    order = [c for c in classes for _ in range(c.count)]
    rnd.shuffle(order)
    members: dict[str, list[str]] = {c.id: [] for c in classes}
    for cls, hour in zip(order, slots, strict=True):
        n += 1
        number = f"INC{prefix}{n:04d}"
        opened = START + timedelta(hours=hour, minutes=rnd.randint(0, 59))
        ci = rnd.choice(cls.cis)
        svc = rnd.choice(cls.words["svc"]) if "svc" in cls.words else ci
        if svc in cls.cis:  # the text names the CI the incident is logged against
            ci = svc
        record = {
            "number": number,
            "sys_id": f"{prefix.lower()}{n:06d}",
            "opened_at": opened.strftime("%Y-%m-%d %H:%M:%S"),
            "resolved_at": (opened + timedelta(minutes=rnd.randint(25, 400))).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "priority": rnd.choice(cls.priorities),
            "cmdb_ci": ci,
            "category": cls.category,
            "assignment_group": "PAY-L2" if system == "system-a" else "DATA-PLATFORM",
            "short_description": _fill(rnd.choice(cls.subjects), rnd, cls, svc),
            "description": _fill(rnd.choice(cls.bodies), rnd, cls, svc),
            "state": "Closed",
            "close_code": "Solved (Permanently)" if cls.cause else "Request fulfilled",
            "close_notes": _fill(rnd.choice(cls.notes), rnd, cls, svc),
        }
        if cls.problem:
            record["problem_id"] = f"PRB{prefix}{classes.index(cls) + 1:03d}"
        if cls.change:
            chg = f"CHG{prefix}{n:04d}"
            record["caused_by"] = chg
            changes.append(
                {
                    "number": chg,
                    "short_description": _fill(cls.change, rnd, cls, svc),
                    "cmdb_ci": svc,
                    "start_date": (opened - timedelta(minutes=rnd.randint(5, 50))).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "type": "Normal",
                    "state": "Closed",
                }
            )
        incidents.append(record)
        members[cls.id].append(number)
    for i, cls in enumerate(classes, 1):
        if cls.problem:
            problems.append(
                {
                    "number": f"PRB{prefix}{i:03d}",
                    "short_description": cls.problem,
                    "state": "Root Cause Analysis",
                    "related_incidents": members[cls.id],
                }
            )
        truth.append(
            {
                "id": cls.id,
                "labels": cls.labels,
                "cause": cls.cause,
                "incidents": members[cls.id],
            }
        )
    for i in range(1, 4):  # routine changes that caused nothing: distractors
        changes.append(
            {
                "number": f"CHG{prefix}9{i:03d}",
                "short_description": f"Routine OS patching wave {i}",
                "cmdb_ci": classes[0].cis[0],
                "start_date": (START + timedelta(days=60 * i)).strftime("%Y-%m-%d %H:%M:%S"),
                "type": "Standard",
                "state": "Closed",
            }
        )
    sn = root / system / "sources" / "servicenow"
    sn.mkdir(parents=True, exist_ok=True)
    header = "# Generated by evals/onboarding/generate_history.py: edit the classes there.\n"
    for name, rows in (("incidents", incidents), ("problems", problems), ("changes", changes)):
        (sn / f"{name}.yaml").write_text(header + yaml.safe_dump(rows, sort_keys=False, width=100))
    (root / system / "truth_incidents.yaml").write_text(
        header + yaml.safe_dump({"incident_classes": truth}, sort_keys=False, width=100)
    )


if __name__ == "__main__":
    generate("system-a", SYSTEM_A, "A", seed=15)
    generate("system-b", SYSTEM_B, "B", seed=16)
