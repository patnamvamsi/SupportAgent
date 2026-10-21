"""WP-107 contract suite: every tool's result matches its declared schema.

Canonical test ID from 11-wp §WP-107: ``test_tool_result_matches_declared_schema``
(one case per tool, against a fake of its API or files).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from supportagent.adapters.fakes.clock import FrozenClock
from supportagent.adapters.tools.airflow import AirflowTool
from supportagent.adapters.tools.cron import CronStatusTool
from supportagent.adapters.tools.gha import GitHubActionsTool
from supportagent.adapters.tools.logs import LogsTool
from supportagent.adapters.tools.oncall import OnCallTool
from supportagent.domain.errors import ToolError
from supportagent.domain.tools import schema_errors

T = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def _client(routes: dict[str, Any], status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, body in routes.items():
            if request.url.path == prefix:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return httpx.Client(base_url="https://fake.test", transport=httpx.MockTransport(handler))


def _airflow() -> tuple[Any, dict[str, object]]:
    client = _client(
        {
            "/api/v1/dags/nightly-sync/dagRuns": {
                "dag_runs": [
                    {"dag_run_id": "r2", "state": "failed", "start_date": "t", "end_date": None},
                    {"dag_run_id": "r1", "state": "success", "start_date": "t", "end_date": "t"},
                ]
            },
            "/api/v1/dags/nightly-sync/dagRuns/r2/taskInstances": {
                "task_instances": [
                    {"task_id": "extract", "state": "failed", "try_number": 3},
                    {"task_id": "load", "state": "upstream_failed", "try_number": 0},
                    {"task_id": "notify", "state": "success", "try_number": 1},
                ]
            },
        }
    )
    return AirflowTool(client=client), {"dag_id": "nightly-sync"}


def _gha() -> tuple[Any, dict[str, object]]:
    client = _client(
        {
            "/repos/acme/payroll-api/actions/runs": {
                "workflow_runs": [
                    {
                        "id": 9,
                        "name": "ci",
                        "status": "completed",
                        "conclusion": "failure",
                        "head_sha": "abc",
                        "created_at": "t",
                    }
                ]
            },
            "/repos/acme/payroll-api/actions/runs/9/jobs": {
                "jobs": [
                    {"name": "test", "conclusion": "failure"},
                    {"name": "lint", "conclusion": "success"},
                ]
            },
        }
    )
    return GitHubActionsTool(client=client), {"repo": "github:acme/payroll-api"}


def _oncall_pd() -> tuple[Any, dict[str, object]]:
    client = _client(
        {
            "/oncalls": {
                "oncalls": [
                    {"user": {"summary": "Ada"}, "escalation_level": 1, "start": "t", "end": "t"}
                ]
            }
        }
    )
    return OnCallTool(pagerduty=client), {"schedule": "pagerduty:PSCHED"}


def _oncall_og() -> tuple[Any, dict[str, object]]:
    client = _client(
        {"/v2/schedules/sched-1/on-calls": {"data": {"onCallParticipants": [{"name": "Bob"}]}}}
    )
    return OnCallTool(opsgenie=client), {"schedule": "opsgenie:sched-1"}


@pytest.fixture
def files(tmp_path: Path) -> Path:
    (tmp_path / "logs" / "payroll-api").mkdir(parents=True)
    (tmp_path / "logs" / "payroll-api" / "a.log").write_text("ERROR db timeout\nok\n")
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "nightly-sync.json").write_text(
        json.dumps({"last_run": "2026-10-21T08:00:00+00:00", "exit_code": 1})
    )
    return tmp_path


TOOLS: dict[str, Callable[[Path], tuple[Any, dict[str, object]]]] = {
    "logs": lambda root: (
        LogsTool(root=root / "logs", allowlist=frozenset({"payroll-api"})),
        {"path": "payroll-api", "pattern": "ERROR"},
    ),
    "cron": lambda root: (
        CronStatusTool(status_dir=root / "cron", clock=FrozenClock(start=T)),
        {"job": "nightly-sync"},
    ),
    "airflow": lambda _: _airflow(),
    "gha": lambda _: _gha(),
    "oncall-pagerduty": lambda _: _oncall_pd(),
    "oncall-opsgenie": lambda _: _oncall_og(),
}


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tool_result_matches_declared_schema(name: str, files: Path) -> None:
    tool, args = TOOLS[name](files)
    result = tool.run(args, timeout_s=2)
    assert schema_errors(dict(result.data), tool.result_schema) == []
    assert tool.trust_class.value == "read" and tool.tier.value == "T0"


def test_tool_semantics(files: Path) -> None:
    airflow, args = _airflow()
    assert [t["task_id"] for t in airflow.run(args, timeout_s=2).data["failed_tasks"]] == [
        "extract",
        "load",
    ]
    gha, gargs = _gha()
    assert gha.run(gargs, timeout_s=2).data["failed_jobs"] == [
        {"name": "test", "conclusion": "failure"}
    ]
    cron, cargs = TOOLS["cron"](files)
    data = cron.run(cargs, timeout_s=2).data
    assert (data["exit_code"], data["minutes_since_run"]) == (1, 60)
    assert cron.run({"job": "missing"}, timeout_s=2).data["found"] is False
    with pytest.raises(ToolError):
        cron.run({"job": "../etc/passwd"}, timeout_s=2)


@pytest.mark.parametrize(("status", "retryable"), [(503, True), (429, True), (404, False)])
def test_tool_http_errors_are_typed(status: int, retryable: bool) -> None:
    client = _client({"/repos/acme/x/actions/runs": {}}, status=status)
    with pytest.raises(ToolError) as caught:
        GitHubActionsTool(client=client).run({"repo": "acme/x"}, timeout_s=2)
    assert caught.value.retryable is retryable
