"""Runbooks (ARCH §5.1, §12.1; 11-wp WP-110): executable YAML, validated at load.

A runbook *is* the RCA plan when it applies. Each step tests one taxonomy
hypothesis with one registered tool, using arguments bound from the service
catalog and a CEL ``check`` over the tool's typed result.
``comms_if_supported`` may only address ``internal``: RCA never drives
customer comms in v0.x (ARCH §5, ``test_runbook_customer_comms_rejected``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from supportagent.domain.model.base import DomainModel

Severity = Literal["P1", "P2", "P3", "P4"]


def _all_severities() -> list[Severity]:
    return ["P1", "P2", "P3", "P4"]


class RunbookStep(DomainModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    hypothesis: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    args: dict[str, str | int] = Field(default_factory=dict)
    check: str = Field(min_length=1)
    stop_if: bool = False


class AppliesWhen(DomainModel):
    classification: str = Field(min_length=1)  # prefix-matched to the case label
    severities: list[Severity] = Field(default_factory=_all_severities)


class RcaBudget(DomainModel):
    """ARCH §5 lite budget (prov.): <= 5 tool calls, <= 3k tokens, <= 3 hypotheses."""

    max_tool_calls: int = Field(default=5, ge=1, le=25)
    max_tokens: int = Field(default=3000, ge=0)
    max_hypotheses: int = Field(default=3, ge=1, le=10)


class CommsIfSupported(DomainModel):
    audience: list[str] = Field(min_length=1)
    template: str = Field(min_length=1)

    @field_validator("audience")
    @classmethod
    def _internal_only(cls, value: list[str]) -> list[str]:
        if value != ["internal"]:
            raise ValueError(  # noqa: TRY003
                "comms_if_supported.audience must be [internal]: RCA never drives "
                "customer comms in v0.x (ARCH §5)"
            )
        return value


class Runbook(DomainModel):
    id: str = Field(pattern=r"^runbook:[a-z0-9-]{1,48}$")
    applies_when: AppliesWhen
    budget: RcaBudget = Field(default_factory=RcaBudget)
    steps: list[RunbookStep] = Field(min_length=1, max_length=25)
    comms_if_supported: CommsIfSupported | None = None


class Probe(DomainModel):
    """The registered probe for one hypothesis (no-runbook path, ARCH §5)."""

    tool: str = Field(min_length=1)
    args: dict[str, str | int] = Field(default_factory=dict)
    check: str = Field(min_length=1)


__all__ = ["AppliesWhen", "CommsIfSupported", "Probe", "RcaBudget", "Runbook", "RunbookStep"]
