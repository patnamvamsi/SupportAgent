"""Replay: an ordered, decrypted view of one trace (01 §5, INV-03).

``ReplayService`` is constructed with the ``EventRepository`` port (01 §3):
``replay(trace_id) -> ReplayView`` returns the trace's events in log order —
``(ts_ms, span_id)``, the repository's ordering — with payloads already
decrypted (10-wp WP-004). A trace with no events is a ``NotFound`` (01 §6):
the CLI maps it to exit code 4 (02 §4).

Each event carries a one-line payload summary from the per-kind formatter
registry (10-wp WP-004): one pure function per ``EventKind``, with a generic
canonical-JSON fallback for kinds without a dedicated formatter.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from supportagent.domain.errors import NotFound
from supportagent.domain.events import EventKind
from supportagent.domain.model.event import Event
from supportagent.ports.events import EventRepository

Formatter = Callable[[Mapping[str, object]], str]


def _fmt_ingest(payload: Mapping[str, object]) -> str:
    return f"origin={payload.get('origin', '?')} source={payload.get('source', '?')}"


def _fmt_dedup(payload: Mapping[str, object]) -> str:
    return (
        f"fingerprint={payload.get('fingerprint', '?')} first_seen={payload.get('first_seen', '?')}"
    )


def _fmt_preclass(payload: Mapping[str, object]) -> str:
    # TriageService records the catalog-resolved service (None: unresolved).
    return f"service={payload.get('service', '?')}"


def _fmt_classify(payload: Mapping[str, object]) -> str:
    return (
        f"classification={payload.get('classification', '?')} "
        f"severity={payload.get('severity', '?')}"
    )


def _fmt_decision(payload: Mapping[str, object]) -> str:
    # Triage decisions carry outcome/reason (ARCH §4); other stages (rca,
    # retention) name themselves in ``stage``.
    if "stage" in payload and "outcome" not in payload:
        return f"stage={payload['stage']}"
    parts = [
        f"outcome={payload.get('outcome', '?')}",
        f"reason={payload.get('reason', '?')}",
    ]
    for key in ("stage", "classification", "severity", "route_id"):
        value = payload.get(key)
        if value is not None:
            parts.append(f"{key.removesuffix('_id')}={value}")
    return " ".join(parts)


def _fmt_llm(payload: Mapping[str, object]) -> str:
    # The event's own status column carries ok/error; the payload names the
    # slot, the pinned and served models (BL-010), and an error class.
    parts = [
        f"slot={payload.get('slot', '?')}",
        f"model={payload.get('model', '?')}",
        f"resolved={payload.get('resolved_model', '?')}",
    ]
    if payload.get("error"):
        parts.append(f"error={payload['error']}")
    return " ".join(parts)


def _fmt_redact(payload: Mapping[str, object]) -> str:
    counts = payload.get("counts_by_class")
    if payload.get("error"):
        return f"error={payload['error']}"
    return f"counts={counts}" if counts else "counts=none"


def _fmt_tool_call(payload: Mapping[str, object]) -> str:
    return f"tool={payload.get('tool', '?')} status={payload.get('status', '?')}"


def _fmt_rca_step(payload: Mapping[str, object]) -> str:
    return f"step={payload.get('step', '?')} status={payload.get('status', '?')}"


def _fmt_gate(payload: Mapping[str, object]) -> str:
    return f"proposal={payload.get('proposal_id', '?')} transition={payload.get('transition', '?')}"


def _fmt_send(payload: Mapping[str, object]) -> str:
    # Executor phases (claim, sent, retry, abort) and comms cadence phases.
    parts = [f"phase={payload.get('phase', '?')}"]
    for key in ("external_ref", "attempts", "plan_id", "error"):
        if payload.get(key) is not None:
            parts.append(f"{key.removeprefix('external_')}={payload[key]}")
    return " ".join(parts)


def _fmt_sync(payload: Mapping[str, object]) -> str:
    return f"table={payload.get('table', '?')} rows={payload.get('rows', '?')}"


def _fmt_generic(payload: Mapping[str, object]) -> str:
    body = {k: v for k, v in sorted(payload.items()) if k != "v"}
    return json.dumps(body, ensure_ascii=False)[:120]


#: One-line payload summaries, per kind (10-wp WP-004).
FORMATTERS: Mapping[EventKind, Formatter] = {
    EventKind.INGEST: _fmt_ingest,
    EventKind.DEDUP: _fmt_dedup,
    EventKind.PRECLASS: _fmt_preclass,
    EventKind.CLASSIFY: _fmt_classify,
    EventKind.DECISION: _fmt_decision,
    EventKind.LLM: _fmt_llm,
    EventKind.REDACT: _fmt_redact,
    EventKind.TOOL_CALL: _fmt_tool_call,
    EventKind.RCA_STEP: _fmt_rca_step,
    EventKind.GATE: _fmt_gate,
    EventKind.SEND: _fmt_send,
    EventKind.SYNC: _fmt_sync,
}


def summarize(kind: EventKind, payload: Mapping[str, object]) -> str:
    """One-line payload summary via the per-kind registry (10-wp WP-004)."""
    return FORMATTERS.get(kind, _fmt_generic)(payload)


@dataclass(frozen=True, slots=True)
class ReplayView:
    """The ordered, decrypted events of one trace (01 §5)."""

    trace_id: str
    tenant_id: str
    events: tuple[Event, ...]  # ordered by (ts_ms, span_id)


class ReplayService:
    """``replay(trace_id) -> ReplayView`` (01 §5); relies on INV-03."""

    def __init__(self, *, repo: EventRepository, tenant_id: str | None = None) -> None:
        self._repo = repo
        self._tenant = tenant_id  # BL-015: the API and CLI always pass it

    def replay(self, trace_id: str) -> ReplayView:
        """Ordered, decrypted events for one trace; ``NotFound`` when empty (01 §6)."""
        events = tuple(self._repo.trace_events(trace_id, self._tenant))
        if not events:
            raise NotFound(f"trace not found: {trace_id!r} (no events in the log)")  # noqa: TRY003
        return ReplayView(trace_id=trace_id, tenant_id=events[0].tenant_id, events=events)
