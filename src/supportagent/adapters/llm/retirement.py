"""Retired-model guard (ARCH §9, 11-wp WP-106): a retired snapshot is never called.

Past its retirement date (from the provider deprecation feed), a request
for the model raises ``LLMModelRetired`` instead of reaching the provider.
``LLMClient`` turns that into ``None``, so the slot safe-defaults, which is
the deprecation runbook's last line of defence.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from supportagent.domain.deprecation import is_retired
from supportagent.domain.errors import LLMModelRetired
from supportagent.ports.clock import Clock
from supportagent.ports.llm import LLMProvider, ProviderRequest, ProviderResponse


class RetiredModelGuard:
    def __init__(self, inner: LLMProvider, *, feed: Mapping[str, date], clock: Clock) -> None:
        self._inner = inner
        self._feed = dict(feed)
        self._clock = clock

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        if is_retired(request.model_id, self._feed, self._clock.now().date()):
            raise LLMModelRetired(  # noqa: TRY003
                f"model {request.model_id} retired on {self._feed[request.model_id]}"
            )
        return self._inner.complete_json(request)


__all__ = ["RetiredModelGuard"]
