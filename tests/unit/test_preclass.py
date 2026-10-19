"""WP-010: pre-classifier — ordered rules, first match wins (ARCH §4).

Canonical test ID from 10-wp §WP-010: ``test_preclass_rules_ordered``.
"""

from __future__ import annotations

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
