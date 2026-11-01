"""ToolRegistry (ARCH §7, 01 §3; 11-wp WP-107): the only way a tool is called.

- **Load time:** every tool's trust class must agree with its tier
  (``class_agrees_with_tier``; a ``read`` tool at T2, or a ``state-changing``
  one at T0, is a ``ConfigError``), and names are unique. v0.1 tools are all
  ``read`` / T0.
- **Call time:** arguments are bound from the case's catalog service
  (``domain.tools.bind_args``). The result must match the tool's declared
  schema, and a result that doesn't is a ``ToolError``, never data the RCA
  reasons over. Every call, success or failure, records a ``tool_call``
  event with **redacted** arguments.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from supportagent.domain.errors import ConfigError, ToolError
from supportagent.domain.events import EventKind
from supportagent.domain.ids import IdPrefix
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.model.event import Event
from supportagent.domain.tiers import class_agrees_with_tier
from supportagent.domain.tools import bind_args, schema_errors
from supportagent.domain.trace import TraceContext
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.tools import ReplayableTool, Tool, ToolResult

DEFAULT_TIMEOUT_S = 10.0


def _past(tool: object) -> ReplayableTool | None:
    return tool if isinstance(tool, ReplayableTool) else None


class ToolRegistry:
    def __init__(  # noqa: PLR0913 — the tools plus the event-write ports
        self,
        tools: Sequence[Tool],
        *,
        events: EventSink,
        ids: IdGenerator,
        clock: Clock,
        redact: Callable[[str], str],
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        by_name: dict[str, Tool] = {}
        for tool in tools:
            if not class_agrees_with_tier(tool.trust_class, tool.tier):
                raise ConfigError(  # noqa: TRY003
                    f"tool {tool.name}: class {tool.trust_class.value!r} can't hold tier "
                    f"{tool.tier.value} (ARCH §7)"
                )
            if tool.name in by_name:
                raise ConfigError(f"tool {tool.name} registered twice")  # noqa: TRY003
            by_name[tool.name] = tool
        self._tools = by_name
        self._events = events
        self._ids = ids
        self._clock = clock
        self._redact = redact
        self._timeout_s = timeout_s

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def call(
        self,
        name: str,
        template: Mapping[str, object],
        service: ServiceEntry,
        trace: TraceContext,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"tool {name!r} is not registered")  # noqa: TRY003
        return self._run(tool, template, service, trace, lambda a, t: tool.run(a, timeout_s=t))

    def replayable(self, name: str) -> bool:
        """ADR-0018: whether the tool can read the past (``run_as_of``)."""
        return _past(self._tools.get(name)) is not None

    def call_as_of(
        self,
        name: str,
        template: Mapping[str, object],
        service: ServiceEntry,
        trace: TraceContext,
        *,
        at: datetime,
    ) -> ToolResult:
        """ADR-0018: the same read as ``call``, ending at ``at``; bound, checked, audited."""
        past = _past(self._tools.get(name))
        if past is None:
            raise ToolError(f"tool {name!r} can't read the past")  # noqa: TRY003
        return self._run(
            self._tools[name],
            template,
            service,
            trace,
            lambda a, t: past.run_as_of(a, at=at, timeout_s=t),
        )

    def _run(
        self,
        tool: Tool,
        template: Mapping[str, object],
        service: ServiceEntry,
        trace: TraceContext,
        run: Callable[[Mapping[str, object], float], ToolResult],
    ) -> ToolResult:
        name = tool.name
        args = bind_args(template, service, literal_args=tool.literal_args)
        started = self._clock.now()
        try:
            result = run(args, self._timeout_s)
            problems = schema_errors(dict(result.data), tool.result_schema)
            if problems:
                raise ToolError(  # noqa: TRY003, TRY301 — an off-schema result is a failure
                    f"tool {name}: result doesn't match its declared schema: {problems[:3]}"
                )
        except ToolError as exc:
            self._emit(name, args, trace, started, status="error", error=str(exc)[:200])
            raise
        self._emit(name, args, trace, started, status="ok", truncated=result.truncated)
        return result

    def _emit(  # noqa: PLR0913 — the event's fields
        self,
        name: str,
        args: Mapping[str, object],
        trace: TraceContext,
        started: datetime,
        *,
        status: str,
        error: str | None = None,
        truncated: bool = False,
    ) -> None:
        ended = self._clock.now()
        latency = int((ended - started).total_seconds() * 1000)
        event_id = self._ids.new(IdPrefix.EVENT)
        payload: dict[str, object] = {
            "v": 1,
            "tool": name,
            "args": {k: self._redact(str(v)) for k, v in sorted(args.items())},
            "truncated": truncated,
        }
        if error is not None:
            payload["error"] = error
        self._events.append(
            Event(
                id=event_id,
                tenant_id=trace.tenant_id,
                trace_id=trace.trace_id,
                span_id=f"tool_{event_id}",
                parent_span_id=trace.span_id,
                kind=EventKind.TOOL_CALL,
                actor="agent",
                latency_ms=max(0, latency),
                status="ok" if status == "ok" else "error",
                payload=payload,
                ts=ended,
            )
        )


__all__ = ["DEFAULT_TIMEOUT_S", "ToolRegistry"]
