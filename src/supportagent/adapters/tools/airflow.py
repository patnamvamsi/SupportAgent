"""``airflow.dag_runs`` (ARCH §7): a DAG's recent runs and the latest failed run's tasks.

Airflow stable REST API v1: ``GET /api/v1/dags/{dag_id}/dagRuns`` and
``…/dagRuns/{run_id}/taskInstances``. The DAG id is bound from
``$service.dags``.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

import httpx

from supportagent.adapters.tools._http import get_json
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.tools import ToolResult

SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["dag_id", "runs", "failed_tasks"],
    "properties": {
        "dag_id": {"type": "string"},
        "runs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["run_id", "state"],
                "properties": {
                    "run_id": {"type": "string"},
                    "state": {"type": "string"},
                    "start_date": {"type": ["string", "null"]},
                    "end_date": {"type": ["string", "null"]},
                },
            },
        },
        "failed_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["task_id", "state"],
                "properties": {
                    "task_id": {"type": "string"},
                    "state": {"type": "string"},
                    "try_number": {"type": ["integer", "null"]},
                },
            },
        },
    },
}


class AirflowTool:
    name = "airflow.dag_runs"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = SCHEMA
    literal_args: frozenset[str] = frozenset()

    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        dag = str(args["dag_id"])
        limit = min(int(str(args.get("limit", 5))), 25)
        base = f"/api/v1/dags/{quote(dag, safe='')}/dagRuns"
        body = get_json(
            self._client,
            base,
            params={"order_by": "-execution_date", "limit": limit},
            timeout_s=timeout_s,
        )
        raw = body.get("dag_runs", []) if isinstance(body, dict) else []
        runs = [
            {
                "run_id": str(r.get("dag_run_id")),
                "state": str(r.get("state")),
                "start_date": r.get("start_date"),
                "end_date": r.get("end_date"),
            }
            for r in raw
            if isinstance(r, dict)
        ]
        failed: list[dict[str, object]] = []
        latest_failed = next((r for r in runs if r["state"] == "failed"), None)
        if latest_failed is not None:
            tasks = get_json(
                self._client,
                f"{base}/{quote(str(latest_failed['run_id']), safe='')}/taskInstances",
                timeout_s=timeout_s,
            )
            items = tasks.get("task_instances", []) if isinstance(tasks, dict) else []
            failed = [
                {
                    "task_id": str(t.get("task_id")),
                    "state": str(t.get("state")),
                    "try_number": t.get("try_number"),
                }
                for t in items
                if isinstance(t, dict) and t.get("state") in ("failed", "upstream_failed")
            ]
        return ToolResult({"dag_id": dag, "runs": runs, "failed_tasks": failed})
