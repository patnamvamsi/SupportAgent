"""Model deprecation notices (ARCH §9 deprecation runbook, 11-wp WP-106).

A provider's deprecation feed maps a pinned model id to its retirement
date. A slot whose model retires within 90 days gets a ``warn`` notice (the
runbook starts at T-90); one past its date is ``retired``, and calls to it
safe-default instead of reaching a model that no longer exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

WARN_DAYS = 90


@dataclass(frozen=True, slots=True)
class DeprecationNotice:
    slot: str
    model_id: str
    retires_on: date
    days_left: int
    status: Literal["warn", "retired"]


def deprecation_notices(
    slots: Mapping[str, str], feed: Mapping[str, date], today: date
) -> list[DeprecationNotice]:
    """A notice per slot whose model retires within ``WARN_DAYS`` (or already has)."""
    notices: list[DeprecationNotice] = []
    for slot, model_id in sorted(slots.items()):
        retires_on = feed.get(model_id)
        if retires_on is None:
            continue
        days_left = (retires_on - today).days
        if days_left <= 0:
            notices.append(DeprecationNotice(slot, model_id, retires_on, days_left, "retired"))
        elif days_left <= WARN_DAYS:
            notices.append(DeprecationNotice(slot, model_id, retires_on, days_left, "warn"))
    return notices


def is_retired(model_id: str, feed: Mapping[str, date], today: date) -> bool:
    retires_on = feed.get(model_id)
    return retires_on is not None and today >= retires_on


__all__ = ["WARN_DAYS", "DeprecationNotice", "deprecation_notices", "is_retired"]
