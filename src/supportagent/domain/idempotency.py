"""Idempotency keys (01 §7.4): stable, derived, 32-hex-character.

Each authorization path derives one key from the authorization's identity and
the action it authorizes. The key is UNIQUE in the outbox (03 §2.3, INV-04),
so re-running the same authorization cannot create a second send. Keys are the
first 32 hex characters of ``sha256`` of a namespaced, pipe-joined string.

The domain owns the derivation so that ``enqueue`` (app) and any future
replay tool compute the *same* key from the *same* inputs (01 §7.4).
"""

from __future__ import annotations

import hashlib

_KEY_LEN = 32


def _first32(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_KEY_LEN]


def approval_key(approval_id: str) -> str:
    """Key for the approval path (01 §7.4): ``sha256("appr|" + approval_id)``."""
    return _first32(f"appr|{approval_id}")


def grant_key(
    *,
    case_id: str,
    action_kind: str,
    scope: str,
    payload_hash: str,
) -> str:
    """Key for the policy-grant path (01 §7.4)."""
    return _first32(f"grant|{case_id}|{action_kind}|{scope}|{payload_hash}")


def plan_key(
    *,
    plan_id: str,
    audience: str,
    trigger: str,
    interval_slot: int,
) -> str:
    """Key for the comms-plan path (01 §7.4)."""
    return _first32(f"plan|{plan_id}|{audience}|{trigger}|{interval_slot}")


__all__ = ["approval_key", "grant_key", "plan_key"]
