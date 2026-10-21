"""``logs.grep`` (ARCH §7): grep a service's allowlisted log paths, at most 1,000 lines.

Two independent fences keep the tool inside the logs it may read:

1. the path must be one of the catalog's ``log_paths`` (the allowlist), and
2. once resolved, symlinks and ``..`` included, it must stay under the
   configured log root.

A path that fails either is a ``ToolError``. A directory is searched for
``*.log`` / ``*.txt``, newest file first. ``pattern`` is a reviewed runbook
literal, length-limited.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.tools import ToolResult

MAX_LINES = 1000
_MAX_PATTERN = 200

SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["path", "count", "matches"],
    "properties": {
        "path": {"type": "string"},
        "count": {"type": "integer"},
        "matches": {
            "type": "array",
            "maxItems": MAX_LINES,
            "items": {
                "type": "object",
                "required": ["file", "line_no", "text"],
                "properties": {
                    "file": {"type": "string"},
                    "line_no": {"type": "integer"},
                    "text": {"type": "string"},
                },
            },
        },
    },
}


class LogsTool:
    name = "logs.grep"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = SCHEMA
    literal_args = frozenset({"pattern"})

    def __init__(self, *, root: Path, allowlist: frozenset[str]) -> None:
        self._root = root.resolve()
        self._allow = allowlist

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        path = str(args.get("path", ""))
        pattern = str(args.get("pattern", ""))
        limit = min(int(str(args.get("max_lines", MAX_LINES))), MAX_LINES)
        if path not in self._allow:
            raise ToolError(f"logs.grep: {path!r} is not an allowlisted log path")  # noqa: TRY003
        target = (self._root / path).resolve()
        if target != self._root and self._root not in target.parents:
            raise ToolError(f"logs.grep: {path!r} resolves outside the log root")  # noqa: TRY003
        if not pattern or len(pattern) > _MAX_PATTERN:
            raise ToolError("logs.grep: a pattern of 1-200 characters is required")  # noqa: TRY003
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"logs.grep: bad pattern: {exc}") from exc  # noqa: TRY003
        files = self._files(target)
        matches: list[dict[str, object]] = []
        truncated = False
        for file in files:
            with file.open(encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if regex.search(line):
                        if len(matches) >= limit:
                            truncated = True
                            break
                        matches.append(
                            {
                                "file": str(file.relative_to(self._root)),
                                "line_no": line_no,
                                "text": line.rstrip("\n")[:2000],
                            }
                        )
            if truncated:
                break
        return ToolResult({"path": path, "count": len(matches), "matches": matches}, truncated)

    @staticmethod
    def _files(target: Path) -> list[Path]:
        if target.is_file():
            return [target]
        if not target.is_dir():
            return []
        found = [p for p in target.rglob("*") if p.suffix in {".log", ".txt"} and p.is_file()]
        return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)
