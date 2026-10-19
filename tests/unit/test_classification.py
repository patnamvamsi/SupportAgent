"""WP-010: prefix match and calibrated score (ARCH §4.2, ADR-0002, 01 §7.6–7.7).

Canonical test IDs from 10-wp §WP-010:
``test_prefix_match_table``, ``test_calibrated_score_missing_label_zero``,
``test_calibrated_score_n_below_10_zero``, ``test_calibrated_score_wilson``.
"""

from __future__ import annotations

from supportagent.domain.classification import calibrated_score, prefix_match
from supportagent.domain.model.calibration import (
    CalibrationCountRow,
    CalibrationLabelRow,
    CalibrationTable,
)
from supportagent.domain.stats import wilson_lower_bound

_MODEL = "claude-sonnet-4-5-20250929"


def _table(labels: dict[str, CalibrationLabelRow]) -> CalibrationTable:
    return CalibrationTable(
        grade="smoke", snapshot_id="snap-1", model_id=_MODEL, labels=labels
    )


def test_prefix_match_table() -> None:
    # ARCH §4: prefix_match(a, b) is true when the labels are equal or share
    # the same first component (the domain); false when they differ.
    assert prefix_match("network/dns", "network/dns") is True  # equal
    assert prefix_match("network", "network/dns") is True  # parent (domain) of child
    assert prefix_match("network/dns", "network/dhcp") is True  # siblings in one domain
    assert prefix_match("network/dns", "app/web") is False  # different domains
    assert prefix_match("net/dns", "network/dns") is False  # "net" is not the "network" domain


def test_calibrated_score_missing_label_zero() -> None:
    # ADR-0002: a label with no calibration row scores 0.0 (fail closed).
    table = _table(
        {"network/dns": CalibrationLabelRow(agree=CalibrationCountRow(n=35, correct=35))}
    )
    assert calibrated_score(table, "app/web", True) == 0.0
    assert calibrated_score(table, "app/web", False) == 0.0


def test_calibrated_score_n_below_10_zero() -> None:
    # ADR-0002: a row with n < 10 scores 0.0 (insufficient evidence).
    table = _table(
        {"network/dns": CalibrationLabelRow(agree=CalibrationCountRow(n=9, correct=9))}
    )
    assert calibrated_score(table, "network/dns", True) == 0.0
    assert calibrated_score(table, "network/dns", False) == 0.0


def test_calibrated_score_wilson() -> None:
    # ADR-0002: score = wilson_lower_bound(correct, n) of the label's ``agree``
    # row when the pre-classifier agrees, else its ``abstain`` row.
    agree = CalibrationCountRow(n=35, correct=35)
    abstain = CalibrationCountRow(n=30, correct=24)
    table = _table({"network/dns": CalibrationLabelRow(agree=agree, abstain=abstain)})
    assert calibrated_score(table, "network/dns", True) == wilson_lower_bound(35, 35)
    assert calibrated_score(table, "network/dns", False) == wilson_lower_bound(24, 30)
