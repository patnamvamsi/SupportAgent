"""Event kinds for the append-only event log (ARCH §15: exactly 20)."""

from __future__ import annotations

from enum import StrEnum


class EventKind(StrEnum):
    INGEST = "ingest"
    DEDUP = "dedup"
    PRECLASS = "preclass"
    CLASSIFY = "classify"
    DECISION = "decision"
    LLM = "llm"
    REDACT = "redact"
    TOOL_CALL = "tool_call"
    RCA_STEP = "rca_step"
    GATE = "gate"
    SEND = "send"
    SYNC = "sync"
    SYNC_TAKEOVER = "sync_takeover"
    FEEDBACK = "feedback"
    PRIOR_ZEROED = "prior_zeroed"
    PAGE_RECOMMENDED = "page_recommended"
    SPEND_BREAKER = "spend_breaker"
    WEBHOOK_REJECTED = "webhook_rejected"
    EXECUTOR_HASH_MISMATCH = "executor_hash_mismatch"
    SLO_MISS = "slo_miss"
