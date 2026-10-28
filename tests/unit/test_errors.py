"""WP-001: error taxonomy (01 §6) — hierarchy and typed fields."""

from __future__ import annotations

from supportagent.domain.errors import (
    AuthorizationDenied,
    BudgetExhausted,
    ConcurrencyConflict,
    ConfigError,
    ConnectorError,
    DomainValidationError,
    ExternalServiceError,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    NotFound,
    SupportAgentError,
    ToolError,
)


def test_error_taxonomy_hierarchies() -> None:
    assert issubclass(ConfigError, SupportAgentError)
    assert issubclass(DomainValidationError, SupportAgentError)
    assert issubclass(AuthorizationDenied, SupportAgentError)
    assert issubclass(ConcurrencyConflict, SupportAgentError)
    assert issubclass(NotFound, SupportAgentError)
    assert issubclass(ExternalServiceError, SupportAgentError)
    assert issubclass(BudgetExhausted, SupportAgentError)
    for llm_error in (ConnectorError, LLMUnavailable, LLMTimeout, LLMRateLimited, ToolError):
        assert issubclass(llm_error, ExternalServiceError)
    # 01 §6: the base is never raised directly — only its subclasses appear
    # in the taxonomy above.
    assert not issubclass(Exception, SupportAgentError)


def test_authorization_denied_code() -> None:
    default = AuthorizationDenied("no valid authorization")
    assert default.code == "no_valid_authorization"
    self_approval = AuthorizationDenied("INV-01: self-approval", code="self_approval")
    assert self_approval.code == "self_approval"
    assert str(self_approval) == "INV-01: self-approval"


def test_external_service_error_retry_fields() -> None:
    retryable = ExternalServiceError("down", retryable=True, retry_after_s=30.0)
    assert retryable.retryable is True
    assert retryable.retry_after_s == 30.0
    assert ConnectorError("503").retryable is False
    assert LLMTimeout("timed out").retry_after_s is None
