"""Label prefix matching and the calibrated score (ARCH §4.2, ADR-0002)."""

from __future__ import annotations

from collections.abc import Mapping

from supportagent.domain.model.calibration import CalibrationTable
from supportagent.domain.stats import wilson_lower_bound

#: Minimum trials per label row before a calibrated score is trusted (ADR-0002,
#: 01 §7.7): fewer than this is treated as insufficient evidence and scores 0.0.
MIN_CALIBRATION_N = 10


def prefix_match(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` are the same label or share its first component.

    Taxonomy labels are two-component slugs (``<domain>/<category>``). The
    first component (the domain) is the unit of agreement: equal labels, a
    domain and one of its children, and siblings within a domain all match;
    labels in different domains do not.
    """
    return a.split("/", 1)[0] == b.split("/", 1)[0]


def calibrated_score(table: CalibrationTable, label: str, preclass_agrees: bool) -> float:
    """Wilson lower bound for ``label``, fail closed (ADR-0002, 01 §7.7).

    The label's ``agree`` row is scored when the pre-classifier agreed, else
    its ``abstain`` row. A missing label, a missing row, or a row with
    ``n < 10`` scores 0.0 (insufficient evidence) rather than a guess.
    """
    row = table.labels.get(label)
    if row is None:
        return 0.0
    count = row.agree if preclass_agrees else row.abstain
    if count is None or count.n < MIN_CALIBRATION_N:
        return 0.0
    return wilson_lower_bound(count.correct, count.n)


def map_source_classification(
    category: str | None, subcategory: str | None, mapping: Mapping[str, str]
) -> str | None:
    """The taxonomy label for a human-set ServiceNow category (BL-035).

    With no ``servicenow.classification_map`` configured, the label is
    ``category/subcategory`` lower-cased: it only means something when the
    partner's categories already match the taxonomy. With a map, the most
    specific key wins (``category/subcategory``, then ``category``). An
    unmapped value is ``None``: the case isn't counted, rather than counted
    as a disagreement it never was.
    """
    cat = (category or "").strip().lower()
    sub = (subcategory or "").strip().lower()
    if not mapping:
        return f"{cat}/{sub}" if cat and sub else None
    if cat and sub and f"{cat}/{sub}" in mapping:
        return mapping[f"{cat}/{sub}"]
    return mapping.get(cat) if cat else None
