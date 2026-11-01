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
from supportagent.domain.errors import HistoryUnavailable, ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.clock import Clock
from supportagent.ports.tools import ToolResult

_MAX_CHANGES = 25
_DEFAULT_WINDOW_MIN = 180
#: The newest page each source returns (GitHub deployments, ReplicaSets).
_PAGE = 50

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
        return self._changes(args, self._clock.now(), timeout_s, as_of=False)

    def run_as_of(
        self, args: Mapping[str, object], *, at: datetime, timeout_s: float
    ) -> ToolResult:
        """ADR-0018: the changes in the window ending at ``at``, none after it. Both
        sources return their newest page only: when it doesn't reach back to the
        window, the history is unavailable, not empty."""
        return self._changes(args, at, timeout_s, as_of=True)

    def _changes(
        self, args: Mapping[str, object], now: datetime, timeout_s: float, *, as_of: bool
    ) -> ToolResult:
        window = int(str(args.get("window_min", _DEFAULT_WINDOW_MIN)))
        if not 1 <= window <= 7 * 24 * 60:
            raise ToolError("changes.recent: window_min must be 1..10080")  # noqa: TRY003
        since = now - timedelta(minutes=window)
        changes: list[dict[str, object]] = []
        asked = False
        if "repo" in args:
            asked = True
            changes += self._deployments(str(args["repo"]), since, timeout_s, as_of=as_of)
        if "namespace" in args or "selector" in args:
            asked = True
            changes += self._rollouts(
                args.get("namespace"), args.get("selector"), since, timeout_s, as_of=as_of
            )
        if as_of:
            changes = [c for c in changes if (t := _parse(c["at"])) is not None and t <= now]
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

    def _deployments(
        self, repo: str, since: datetime, timeout_s: float, *, as_of: bool = False
    ) -> list[dict[str, object]]:
        if self._github is None:
            raise ToolError("changes.recent: the github source is not configured")  # noqa: TRY003
        name = repo.removeprefix("github:")
        if name.count("/") != 1:
            raise ToolError(f"changes.recent: repo must be owner/name, got {name!r}")  # noqa: TRY003
        body = get_json(
            self._github,
            f"/repos/{name}/deployments",
            params={"per_page": 30},
            timeout_s=timeout_s,
        )
        if as_of:
            _reaches_back(
                body if isinstance(body, list) else [], since, "created_at", "github", page=30
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
        self,
        namespace: object,
        selector: object,
        since: datetime,
        timeout_s: float,
        *,
        as_of: bool = False,
    ) -> list[dict[str, object]]:
        if self._k8s is None:
            raise ToolError("changes.recent: the kubernetes source is not configured")  # noqa: TRY003
        ns, sel = check_scope(namespace, selector, self.name)
        body = get_json(
            self._k8s,
            f"/apis/apps/v1/namespaces/{ns}/replicasets",
            params={"labelSelector": sel, "limit": _PAGE},
            timeout_s=timeout_s,
        )
        items = body.get("items", []) if isinstance(body, dict) else []
        if as_of:
            _reaches_back(
                [i.get("metadata") or {} for i in items if isinstance(i, dict)]
                if isinstance(items, list)
                else [],
                since,
                "creationTimestamp",
                "kubernetes",
            )
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


def _reaches_back(
    items: list[object], since: datetime, key: str, source: str, page: int = _PAGE
) -> None:
    """A full page whose oldest item is newer than ``since`` may be hiding the window."""
    if len(items) < page:
        return
    stamps = [t for i in items if isinstance(i, dict) and (t := _parse(i.get(key)))]
    if stamps and min(stamps) > since:
        raise HistoryUnavailable(  # noqa: TRY003
            f"changes.recent: {source}'s newest {page} changes don't reach back to {since:%Y-%m-%d}"
        )


__all__ = ["SCHEMA", "RecentChangesTool"]
