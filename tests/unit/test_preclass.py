"""WP-010: pre-classifier — ordered rules, first match wins (ARCH §4).

Canonical test ID from 10-wp §WP-010: ``test_preclass_rules_ordered``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supportagent.adapters.files.preclass_loader import load_preclass_rules
from supportagent.adapters.files.taxonomy_loader import load_classification
from supportagent.domain.errors import ConfigError, DomainValidationError
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.preclass import PreclassRule, RuleSet
from tests.builders import an_alert


def _evt(**overrides: object) -> AlertEvent:
    base = an_alert()
    base.update(overrides)
    return AlertEvent.model_validate(base)


def test_preclass_rules_ordered() -> None:
    # ARCH §4: pre-classifier rules are evaluated in order; the first rule whose
    # ``when`` all match wins, and no match yields ``None`` (the abstain path).
    rule_set = RuleSet(
        rules=[
            # Both rules match the subject below; the first (in order) wins.
            PreclassRule(when={"subject": "payment"}, label="payments/api"),
            PreclassRule(when={"subject": "5xx"}, label="payments/5xx"),
        ]
    )
    evt = _evt(subject="payment api 5xx spike")
    assert rule_set.classify(evt) == "payments/api"

    # A subject matching only the second rule takes the second rule.
    only_second = RuleSet(rules=[rule_set.rules[1]]).classify(_evt(subject="dns 5xx spike"))
    assert only_second == "payments/5xx"

    # No rule matches -> None (abstain).
    assert rule_set.classify(_evt(subject="disk full")) is None

    # A rule whose field is absent (None) never matches.
    none_rule = RuleSet(rules=[PreclassRule(when={"cmdb_ci": "CI1001"}, label="network/dns")])
    assert none_rule.classify(_evt(cmdb_ci=None)) is None


def test_shipped_preclass_rules_load_with_taxonomy_labels() -> None:
    # 10-wp WP-010: rules are loaded from taxonomy/preclass_rules.yaml; every
    # label is from the same taxonomy as the LLM (ARCH §4.2).
    rule_set = load_preclass_rules(Path("taxonomy/preclass_rules.yaml"))
    taxonomy = load_classification(Path("taxonomy/classification.yaml"))
    assert rule_set.version is not None
    assert rule_set.rules
    known = {label.id for label in taxonomy.labels}
    assert {rule.label for rule in rule_set.rules} <= known


@pytest.mark.parametrize(
    "when",
    [{"subject": "(unclosed"}, {"subjcet": "down"}, {"__class__": "x"}],
    ids=["bad-regex", "typo-field", "dunder-field"],
)
def test_preclass_rule_invalid_rejected_at_load(when: dict[str, str]) -> None:
    # A broken rule must fail at load, not raise mid-triage or never match.
    with pytest.raises(DomainValidationError):
        PreclassRule(when=when, label="network/latency")


def test_preclass_loader_wraps_errors(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text("rules:\n  - when: {subject: '(unclosed'}\n    label: a/b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="preclass"):
        load_preclass_rules(path)
