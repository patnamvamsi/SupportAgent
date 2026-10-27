"""``changes.recent`` (WP-M04, ADR-0011): what changed for a service, newest first.

A normalized feed over the configured sources: GitHub deployments for the
service's repo, and Kubernetes rollouts (new ReplicaSets) for its namespace and
selector. Each source is optional: a call passes the arguments it has
(``repo``, or ``namespace`` + ``selector``). This is the first change source
of ARCH §7 / CAP §6 item 7 (BL-038) in a shape others can join.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

import httpx

from supportagent.adapters.tools._http import get_json
from supportagent.adapters.tools.kubernetes import check_scope
from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.clock import Clock
from supportagent.ports.tools import ToolResult

_MAX_CHANGES = 25
_DEFAULT_WINDOW_MIN = 180

SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["window_min", "changes", "count"],
    "properties": {
        "window_min": {"type": "integer"},
        "changes": {
            "type": "array",
            "maxItems": _MAX_CHANGES,
            "items": {
                "type": "object",
                "required": ["source", "kind", "ref", "at", "minutes_ago"],
                "properties": {
                    "source": {"type": "string", "enum": ["github", "kubernetes"]},
                    "kind": {"type": "string", "enum": ["deploy", "rollout"]},
                    "ref": {"type": "string"},
                    "at": {"type": "string"},
                    "minutes_ago": {"type": "integer"},
                    "actor": {"type": ["string", "null"]},
                    "environment": {"type": ["string", "null"]},
                },
            },
        },
        "count": {"type": "integer"},
    },
}


def _parse(ts: object) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class RecentChangesTool:
    name = "changes.recent"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = SCHEMA
    literal_args: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        clock: Clock,
        github: httpx.Client | None = None,
        kubernetes: httpx.Client | None = None,
    ) -> None:
        self._clock = clock
        self._github = github
        self._k8s = kubernetes

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        window = int(str(args.get("window_min", _DEFAULT_WINDOW_MIN)))
        if not 1 <= window <= 7 * 24 * 60:
            raise ToolError("changes.recent: window_min must be 1..10080")  # noqa: TRY003
        now = self._clock.now()
        since = now - timedelta(minutes=window)
        changes: list[dict[str, object]] = []
        asked = False
        if "repo" in args:
            asked = True
            changes += self._deployments(str(args["repo"]), since, timeout_s)
        if "namespace" in args or "selector" in args:
            asked = True
            changes += self._rollouts(args.get("namespace"), args.get("selector"), since, timeout_s)
        if not asked:
            raise ToolError("changes.recent: pass repo, or namespace and selector")  # noqa: TRY003
        for change in changes:
            at = _parse(change["at"])
            change["minutes_ago"] = int((now - at).total_seconds() // 60) if at else 0
        changes.sort(key=lambda c: int(str(c["minutes_ago"])))
        return ToolResult(
            {"window_min": window, "changes": changes[:_MAX_CHANGES], "count": len(changes)},
            len(changes) > _MAX_CHANGES,
        )

    def _deployments(self, repo: str, since: datetime, timeout_s: float) -> list[dict[str, object]]:
        if self._github is None:
            raise ToolError("changes.recent: the github source is not configured")  # noqa: TRY003
        name = repo.removeprefix("github:")
        if name.count("/") != 1:
            raise ToolError(f"changes.recent: repo must be owner/name, got {name!r}")  # noqa: TRY003
        body = get_json(
            self._github, f"/repos/{name}/deployments", params={"per_page": 30}, timeout_s=timeout_s
        )
        out: list[dict[str, object]] = []
        for d in body if isinstance(body, list) else []:
            at = _parse(d.get("created_at")) if isinstance(d, dict) else None
            if at is None or at < since:
                continue
            out.append(
                {
                    "source": "github",
                    "kind": "deploy",
                    "ref": str(d.get("sha") or d.get("ref") or "")[:40],
                    "at": at.isoformat(),
                    "actor": (d.get("creator") or {}).get("login"),
                    "environment": d.get("environment"),
                }
            )
        return out

    def _rollouts(
        self, namespace: object, selector: object, since: datetime, timeout_s: float
    ) -> list[dict[str, object]]:
        if self._k8s is None:
            raise ToolError("changes.recent: the kubernetes source is not configured")  # noqa: TRY003
        ns, sel = check_scope(namespace, selector, self.name)
        body = get_json(
            self._k8s,
            f"/apis/apps/v1/namespaces/{ns}/replicasets",
            params={"labelSelector": sel, "limit": 50},
            timeout_s=timeout_s,
        )
        items = body.get("items", []) if isinstance(body, dict) else []
        out: list[dict[str, object]] = []
        for rs in items if isinstance(items, list) else []:
            meta = (rs.get("metadata") or {}) if isinstance(rs, dict) else {}
            at = _parse(meta.get("creationTimestamp"))
            if at is None or at < since:
                continue
            revision = (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision", "")
            out.append(
                {
                    "source": "kubernetes",
                    "kind": "rollout",
                    "ref": f"{meta.get('name', '')}#{revision}",
                    "at": at.isoformat(),
                    "actor": None,
                    "environment": ns,
                }
            )
        return out


__all__ = ["SCHEMA", "RecentChangesTool"]
