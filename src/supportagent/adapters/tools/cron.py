"""``cron.status`` (ARCH §7): a scheduled job's last run, **status only**.

Re-invoking cron is a code-execution capability, deferred until a constrained
runner exists (ARCH §7). The customer's cron wrapper writes
``<status_dir>/<job>.json``, ``{"last_run": ISO-8601, "exit_code": int}``.
The job name is bound from ``$service.dags`` and may not escape the directory.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.clock import Clock
from supportagent.ports.tools import ToolResult

_JOB = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["job", "found"],
    "properties": {
        "job": {"type": "string"},
        "found": {"type": "boolean"},
        "last_run": {"type": ["string", "null"]},
        "exit_code": {"type": ["integer", "null"]},
        "minutes_since_run": {"type": ["integer", "null"]},
    },
}


class CronStatusTool:
    name = "cron.status"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = SCHEMA
    literal_args: frozenset[str] = frozenset()

    def __init__(self, *, status_dir: Path, clock: Clock) -> None:
        self._dir = status_dir
        self._clock = clock

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        job = str(args["job"]).removeprefix("cron:")
        if not _JOB.match(job):
            raise ToolError(f"cron.status: bad job name {job!r}")  # noqa: TRY003
        path = self._dir / f"{job}.json"
        if not path.is_file():
            return ToolResult(
                {
                    "job": job,
                    "found": False,
                    "last_run": None,
                    "exit_code": None,
                    "minutes_since_run": None,
                }
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            last = datetime.fromisoformat(str(data["last_run"]))
            code = int(data["exit_code"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ToolError(f"cron.status: unreadable status for {job}: {exc}") from exc  # noqa: TRY003
        minutes = int((self._clock.now() - last).total_seconds() // 60)
        return ToolResult(
            {
                "job": job,
                "found": True,
                "last_run": last.isoformat(),
                "exit_code": code,
                "minutes_since_run": minutes,
            }
        )
