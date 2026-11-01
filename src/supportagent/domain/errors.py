"""Error taxonomy (01 §6): one base, typed subclasses, no bare Exception."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import ValidationError


class SupportAgentError(Exception):
    """Base for every supportagent error. Never raised directly (01 §6)."""


class ConfigError(SupportAgentError):
    """Configuration is missing, invalid, or fails the startup check."""


class DomainValidationError(SupportAgentError, ValidationError):
    """A domain rule was violated (bad transition, float in a hashed payload).

    Also a pydantic ``ValidationError`` so a domain-rule violation surfaces as
    the pinned validation API (01 §7.1, 10-wp WP-004) while remaining a typed
    ``SupportAgentError`` for application-level handling (01 §6). It is raised
    from user-level code (model ``__init__``, state machines, hashing) — never
    from inside a pydantic validator — so its identity is not re-wrapped.
    """

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        # A bare message becomes a single value_error line (01 §7.1).
        if len(args) == 1 and isinstance(args[0], str) and not kwargs:
            msg = str(args[0])
            return cls.from_exception_data(
                title=cls.__name__,
                line_errors=[
                    {
                        "type": "value_error",
                        "loc": (),
                        "input": None,
                        "ctx": {"error": ValueError(msg)},
                    }
                ],
            )
        return ValidationError.__new__(cls, *args, **kwargs)

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args)


class ConcurrencyConflict(SupportAgentError):
    """A concurrent write lost the optimistic-concurrency race."""


class NotFound(SupportAgentError):
    """A referenced entity does not exist (or is another tenant's)."""


class BudgetExhausted(SupportAgentError):
    """The tenant's LLM/tool budget is exhausted (spend breaker, 01 §7.3)."""


DenialCode = Literal[
    "no_valid_authorization", "self_approval", "immutable", "rbac", "stale_proposal"
]


class AuthorizationDenied(SupportAgentError):
    """An authorization rule rejected the action (01 §6, INV-01)."""

    code: DenialCode

    def __init__(
        self,
        message: str,
        *,
        code: Literal[
            "no_valid_authorization", "self_approval", "immutable", "rbac", "stale_proposal"
        ] = ("no_valid_authorization"),
    ) -> None:
        super().__init__(message)
        self.code = code


class AuthenticationFailed(SupportAgentError):
    """The caller's identity could not be established (OIDC login, session; 02 §2).

    Distinct from ``AuthorizationDenied``: the API maps this to 401, that to 403.
    """


class ExternalServiceError(SupportAgentError):
    """An external dependency failed; retryability is typed, not guessed."""

    retryable: bool
    retry_after_s: float | None

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_s = retry_after_s


class ConnectorError(ExternalServiceError):
    """An incident-system call failed (01 §6, WP-M07). ``status`` is the HTTP status, if any.

    Each connector raises its own subclass (``adapters.servicenow.errors``);
    core code only ever catches this.
    """

    status: int | None

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_s: float | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message, retryable=retryable, retry_after_s=retry_after_s)
        self.status = status


class LLMUnavailable(ExternalServiceError):
    """The LLM provider is down or unreachable."""


class LLMCircuitOpen(LLMUnavailable):
    """The LLM circuit breaker is open: degraded mode, no call made (11-wp WP-106)."""


class LLMModelRetired(LLMUnavailable):
    """The slot's pinned model is past its retirement date (ARCH §9)."""


class LLMTimeout(ExternalServiceError):
    """The LLM call exceeded its deadline (01 §7.2)."""


class LLMRateLimited(ExternalServiceError):
    """The LLM provider rate-limited the request (429)."""


class ToolError(ExternalServiceError):
    """A tool execution failed after its retries."""


class HistoryUnavailable(ToolError):
    """ADR-0018: the store no longer holds the past this read asked for (aged out, or
    the newest page of history doesn't reach back that far). Reported, never guessed."""
