"""``gha.recent_runs`` (ARCH §7): a repo's recent workflow runs and the latest failure's jobs.

GitHub REST: ``GET /repos/{owner}/{repo}/actions/runs`` and
``…/actions/runs/{id}/jobs``. The repo is bound from ``$service.repos``
(``github:owner/name`` or ``owner/name``).
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from supportagent.adapters.tools._http import get_json
from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.tools import ToolResult

SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["repo", "runs", "failed_jobs"],
    "properties": {
        "repo": {"type": "string"},
        "runs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "status"],
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": ["string", "null"]},
                    "status": {"type": "string"},
                    "conclusion": {"type": ["string", "null"]},
                    "head_sha": {"type": ["string", "null"]},
                    "created_at": {"type": ["string", "null"]},
                },
            },
        },
        "failed_jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "conclusion": {"type": ["string", "null"]},
                },
            },
        },
    },
}


class GitHubActionsTool:
    name = "gha.recent_runs"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = SCHEMA
    literal_args: frozenset[str] = frozenset()

    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        repo = str(args["repo"]).removeprefix("github:")
        if repo.count("/") != 1:
            raise ToolError(f"gha.recent_runs: repo must be owner/name, got {repo!r}")  # noqa: TRY003
        limit = min(int(str(args.get("limit", 5))), 25)
        body = get_json(
            self._client,
            f"/repos/{repo}/actions/runs",
            params={"per_page": limit},
            timeout_s=timeout_s,
        )
        raw = body.get("workflow_runs", []) if isinstance(body, dict) else []
        runs = [
            {
                "id": int(r.get("id", 0)),
                "name": r.get("name"),
                "status": str(r.get("status")),
                "conclusion": r.get("conclusion"),
                "head_sha": r.get("head_sha"),
                "created_at": r.get("created_at"),
            }
            for r in raw
            if isinstance(r, dict)
        ]
        failed: list[dict[str, object]] = []
        latest = next((r for r in runs if r["conclusion"] == "failure"), None)
        if latest is not None:
            jobs = get_json(
                self._client,
                f"/repos/{repo}/actions/runs/{latest['id']}/jobs",
                params={"filter": "latest"},
                timeout_s=timeout_s,
            )
            items = jobs.get("jobs", []) if isinstance(jobs, dict) else []
            failed = [
                {"name": str(j.get("name")), "conclusion": j.get("conclusion")}
                for j in items
                if isinstance(j, dict) and j.get("conclusion") == "failure"
            ]
        return ToolResult({"repo": repo, "runs": runs, "failed_jobs": failed})
