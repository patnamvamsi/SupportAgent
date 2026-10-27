"""``k8s.pod_status`` (WP-M04, ADR-0011): a service's pods, read-only.

Namespace and label selector come from the catalog (``k8s_namespace``,
``k8s_selector``), never from a ticket. The token needs ``get``/``list`` on
pods (and on replicasets for ``changes.recent``) in that namespace only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import httpx

from supportagent.adapters.tools._http import get_json
from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.tools import ToolResult

_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_SELECTOR = re.compile(r"^[A-Za-z0-9._/=,!-]{1,200}$")
_MAX_PODS = 50

SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["namespace", "selector", "pods", "restarts_total", "oomkilled", "not_ready"],
    "properties": {
        "namespace": {"type": "string"},
        "selector": {"type": "string"},
        "pods": {
            "type": "array",
            "maxItems": _MAX_PODS,
            "items": {
                "type": "object",
                "required": ["name", "phase", "ready", "restarts"],
                "properties": {
                    "name": {"type": "string"},
                    "phase": {"type": "string"},
                    "ready": {"type": "boolean"},
                    "restarts": {"type": "integer"},
                    "last_termination": {"type": ["string", "null"]},
                },
            },
        },
        "restarts_total": {"type": "integer"},
        "oomkilled": {"type": "integer"},
        "not_ready": {"type": "integer"},
    },
}


def check_scope(namespace: object, selector: object, tool: str) -> tuple[str, str]:
    if not isinstance(namespace, str) or not _NAMESPACE.match(namespace):
        raise ToolError(f"{tool}: namespace must be bound from $service.k8s_namespace")  # noqa: TRY003
    if not isinstance(selector, str) or not _SELECTOR.match(selector):
        raise ToolError(f"{tool}: selector must be bound from $service.k8s_selector")  # noqa: TRY003
    return namespace, selector


class PodStatusTool:
    name = "k8s.pod_status"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = SCHEMA
    literal_args: frozenset[str] = frozenset()

    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        namespace, selector = check_scope(args.get("namespace"), args.get("selector"), self.name)
        body = get_json(
            self._client,
            f"/api/v1/namespaces/{namespace}/pods",
            params={"labelSelector": selector, "limit": _MAX_PODS},
            timeout_s=timeout_s,
        )
        items = body.get("items", []) if isinstance(body, dict) else []
        pods: list[dict[str, object]] = []
        oom = 0
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            status = item.get("status") or {}
            containers = status.get("containerStatuses") or []
            restarts = sum(int(c.get("restartCount", 0)) for c in containers if isinstance(c, dict))
            reasons = [
                ((c.get("lastState") or {}).get("terminated") or {}).get("reason")
                for c in containers
                if isinstance(c, dict)
            ]
            last = next((r for r in reasons if r), None)
            oom += sum(1 for r in reasons if r == "OOMKilled")
            pods.append(
                {
                    "name": str((item.get("metadata") or {}).get("name", "")),
                    "phase": str(status.get("phase", "Unknown")),
                    "ready": bool(containers)
                    and all(bool(c.get("ready")) for c in containers if isinstance(c, dict)),
                    "restarts": restarts,
                    "last_termination": last,
                }
            )
        return ToolResult(
            {
                "namespace": namespace,
                "selector": selector,
                "pods": pods,
                "restarts_total": sum(int(str(p["restarts"])) for p in pods),
                "oomkilled": oom,
                "not_ready": sum(1 for p in pods if not p["ready"]),
            }
        )


__all__ = ["SCHEMA", "PodStatusTool", "check_scope"]
