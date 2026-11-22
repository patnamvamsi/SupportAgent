"""A token budget on one onboarding run's model calls (ADR-0023 §3, the manifest's ``budget``).

Sits inside the response cache, so only calls that reach the model count.
Once the tokens spent reach the limit, every further call is refused as
:class:`LLMUnavailable`: the slot answers ``None``, and the step records what
it couldn't do so the next run picks it up.
"""

from __future__ import annotations

from supportagent.domain.errors import LLMUnavailable
from supportagent.ports.llm import LLMProvider, ProviderRequest, ProviderResponse


class TokenBudgetSpent(LLMUnavailable):
    """The run's ``budget.llm_tokens`` is spent."""


class BudgetedProvider:
    def __init__(self, inner: LLMProvider, *, limit: int) -> None:
        self._inner = inner
        self.limit = limit
        self.spent = 0
        self.refused = 0

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        if self.exhausted:
            self.refused += 1
            raise TokenBudgetSpent(  # noqa: TRY003
                f"the onboarding token budget is spent ({self.spent} of {self.limit})"
            )
        response = self._inner.complete_json(request)
        self.spent += response.tokens_in + response.tokens_out
        return response


__all__ = ["BudgetedProvider", "TokenBudgetSpent"]
