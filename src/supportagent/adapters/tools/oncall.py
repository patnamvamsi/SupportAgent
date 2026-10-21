"""``oncall.who`` (ARCH §14, §7): who is on call for a service's schedule.

The schedule is bound from ``$service.oncall_schedule``: ``pagerduty:<id>``
(``GET /oncalls?schedule_ids[]=<id>``) or ``opsgenie:<id>``
(``GET /v2/schedules/<id>/on-calls``). It's a read: the agent shows who is
on call; it never pages (ARCH §14).
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
    "required": ["schedule", "provider", "oncall"],
    "properties": {
        "schedule": {"type": "string"},
        "provider": {"type": "string", "enum": ["pagerduty", "opsgenie"]},
        "oncall": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "level": {"type": ["integer", "null"]},
                    "start": {"type": ["string", "null"]},
                    "end": {"type": ["string", "null"]},
                },
            },
        },
    },
}


class OnCallTool:
    name = "oncall.who"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = SCHEMA
    literal_args: frozenset[str] = frozenset()

    def __init__(
        self, *, pagerduty: httpx.Client | None = None, opsgenie: httpx.Client | None = None
    ) -> None:
        self._pd = pagerduty
        self._og = opsgenie

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        schedule = str(args["schedule"])
        provider, _, ident = schedule.partition(":")
        if provider == "pagerduty" and self._pd is not None:
            body = get_json(
                self._pd, "/oncalls", params={"schedule_ids[]": ident}, timeout_s=timeout_s
            )
            rows = body.get("oncalls", []) if isinstance(body, dict) else []
            people = [
                {
                    "name": str((r.get("user") or {}).get("summary", "unknown")),
                    "level": r.get("escalation_level"),
                    "start": r.get("start"),
                    "end": r.get("end"),
                }
                for r in rows
                if isinstance(r, dict)
            ]
        elif provider == "opsgenie" and self._og is not None:
            body = get_json(self._og, f"/v2/schedules/{ident}/on-calls", timeout_s=timeout_s)
            data = body.get("data", {}) if isinstance(body, dict) else {}
            names = data.get("onCallParticipants", []) if isinstance(data, dict) else []
            people = [
                {"name": str(p.get("name")), "level": None, "start": None, "end": None}
                for p in names
                if isinstance(p, dict)
            ]
        else:
            raise ToolError(f"oncall.who: no client for schedule {schedule!r}")  # noqa: TRY003
        return ToolResult({"schedule": schedule, "provider": provider, "oncall": people})
