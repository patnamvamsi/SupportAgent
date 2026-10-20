"""Eval case format (04 §8): ``evals/cases/<id>.yaml``.

```yaml
id: db-sat-01                      # = the file stem
source: synthetic                  # where the case came from (ROADMAP §2.1)
tags: [routing]                    # `invariant` puts it in the invariants suite
modes: [fixture, live]             # where it is meaningful (a scripted fault is fixture-only)
repeat: 2                          # deliver the alert N times; the last delivery is scored
input: {...}                       # an AlertEvent; tenant/source/table/received_at/raw_ref default
fixture: {classification, severity} | {raw: "..."} | {fault: timeout}
                                   # the fixture LLM's answer; defaults to the expected labels
expect:                            # only the fields set are scored
  outcome: routed | safe_default | linked
  reason: classifier-conflict      # the TriageDecision reason
  classification: database/saturation   # prefix match (ARCH §4.2), and the calibration truth
  severity: P2                     # exact
  actions: [work_note]             # set equality over the recorded proposals
  not_acted_prefix: request/       # the label acted on (routed only) must not start with this
  absent_from_llm: [AKIA...]       # strings that must never reach the provider (INV-06)
meta: {why: "..."}                 # required when a case is edited (ROADMAP §2.1)
```
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from supportagent.domain.model.alert import AlertEvent

Mode = Literal["fixture", "live"]
_ALL_MODES: tuple[Mode, ...] = ("fixture", "live")
_BASE = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FixtureSpec(_Strict):
    classification: str | None = None
    severity: str | None = None
    raw: str | None = None
    fault: Literal["timeout", "unavailable", "rate_limited"] | None = None


class Expect(_Strict):
    outcome: Literal["routed", "safe_default", "linked"] | None = None
    reason: str | None = None
    classification: str | None = None
    severity: str | None = None
    actions: list[str] | None = None
    not_acted_prefix: str | None = None
    absent_from_llm: list[str] = Field(default_factory=list)


class EvalCase(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    source: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    modes: list[Mode] = Field(default_factory=lambda: list(_ALL_MODES))
    input: dict[str, Any]
    fixture: FixtureSpec | None = None
    repeat: int = Field(default=1, ge=1, le=5)
    expect: Expect
    meta: dict[str, str] = Field(default_factory=dict)

    def alert(self, index: int) -> AlertEvent:
        """The case's AlertEvent; each case is received one minute after the previous."""
        data: dict[str, Any] = {
            "tenant_id": "acme",
            "source": "servicenow",
            "origin": "alert",
            "table": "incident",
            "ticket_number": None,
            "resource": None,
            "alert_type": None,
            "error_signature": None,
            "severity_hint": None,
            "cmdb_ci": None,
            "received_at": _BASE + timedelta(minutes=index),
            "external_id": f"sys-{self.id}",
            "raw_ref": f"eval:{self.id}",
            **self.input,
        }
        return AlertEvent.model_validate_json(json.dumps(data, default=_iso))

    def fixture_answer(self) -> FixtureSpec:
        """What the fixture LLM says: the case's script, else the expected labels."""
        if self.fixture is not None:
            return self.fixture
        return FixtureSpec(classification=self.expect.classification, severity=self.expect.severity)


def _iso(value: datetime) -> str:
    return value.isoformat()


def parse_cases(
    documents: Iterable[tuple[str, Mapping[str, Any]]], *, tag: str | None = None
) -> list[EvalCase]:
    """Validate ``(file stem, YAML document)`` pairs, in order; ``tag`` filters.

    The files are read by ``adapters/files`` (ADR-0004: YAML parsing stays at
    the file boundary); this module only validates the parsed documents.
    """
    cases: list[EvalCase] = []
    for stem, data in documents:
        case = EvalCase.model_validate(data)
        if case.id != stem:
            raise ValueError(f"case {stem}: id {case.id!r} must equal the file name")  # noqa: TRY003
        if tag is None or tag in case.tags:
            cases.append(case)
    return cases


__all__ = ["EvalCase", "Expect", "FixtureSpec", "Mode", "parse_cases"]
