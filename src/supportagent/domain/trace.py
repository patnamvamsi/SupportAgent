"""TraceContext: explicit trace/span identity (01 §4).

A trace spans one unit of work of the system for one tenant; a span is one
step of that work. ``TraceContext`` is a frozen dataclass
``(tenant_id, trace_id, span_id)`` (01 §4) with a ``child()`` method that
links a new span under the same trace. It is **passed explicitly** to every
step that emits events — never stored in globals or context variables.

The domain is pure (01 §2: no clocks, no randomness), so trace and span ids
are minted by the caller (an adapter or the entry point) and passed in;
``new`` and ``child`` only carry and link them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TraceContext:
    """One span of one tenant's trace (01 §4).

    ``parent_span_id`` is the span this span was derived from, or ``None``
    for the root span of the trace.
    """

    tenant_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None

    @classmethod
    def new(cls, tenant_id: str, trace_id: str, span_id: str) -> TraceContext:
        """A root span for one tenant's trace (no parent)."""
        return cls(tenant_id=tenant_id, trace_id=trace_id, span_id=span_id)

    def child(self, span_id: str) -> TraceContext:
        """A new span in the same trace, parent-linked to this span (01 §4)."""
        return TraceContext(
            tenant_id=self.tenant_id,
            trace_id=self.trace_id,
            span_id=span_id,
            parent_span_id=self.span_id,
        )
