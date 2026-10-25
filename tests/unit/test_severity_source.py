"""ADR-0009: severity comes from the source priority, through a pluggable strategy."""

from __future__ import annotations

import pytest

from supportagent import bootstrap
from supportagent.adapters.servicenow.mapping import severity_from_priority
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.severity import Severity, SourcePrioritySeverity
from tests.builders import an_alert
from tests.unit.test_triage_service import _shadow_config


@pytest.mark.parametrize(
    ("priority", "expected"),
    [
        ("1", Severity.P1),
        ("2", Severity.P2),
        ("3", Severity.P3),
        ("4", Severity.P4),
        ("5", Severity.P4),
        ("0", None),
        ("", None),
        (None, None),
    ],
)
def test_servicenow_priority_maps_to_severity(
    priority: str | None, expected: Severity | None
) -> None:
    assert severity_from_priority(priority) == expected


def test_source_priority_wins_and_llm_is_the_fallback() -> None:
    strategy = SourcePrioritySeverity()
    with_hint = AlertEvent.model_validate(an_alert(severity_hint=Severity.P2))
    without = AlertEvent.model_validate(an_alert(severity_hint=None))
    assert strategy.resolve(with_hint, Severity.P4) == (Severity.P2, "source_priority")
    assert strategy.resolve(without, Severity.P4) == (Severity.P4, "llm")


def test_config_defaults_to_source_priority() -> None:
    config = _shadow_config()
    assert config.severity.strategy == "source_priority"
    assert isinstance(bootstrap._severity_strategy(config), SourcePrioritySeverity)
