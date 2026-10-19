"""Pre-classifier rule models (ARCH §4).

The ``DomainModel`` subclasses for the ordered ``when``/``label``
pre-classifier. Re-exported by :mod:`supportagent.domain.preclass` to preserve
that public import path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import Field

from supportagent.domain.model.base import DomainModel

_MIN_RULE_FIELDS = 1


class PreclassRule(DomainModel):
    """One ordered rule: every ``when`` field/regex must match, then ``label``.

    ``when`` maps an ``AlertEvent`` field name to a regular expression matched
    with :func:`re.search`. A field whose value is absent (``None``) never
    matches. Rules are evaluated in list order; the first to match wins.
    """

    when: dict[str, str] = Field(min_length=_MIN_RULE_FIELDS)
    label: str = Field(min_length=1)


class RuleSet(DomainModel):
    """The ordered pre-classifier rules for a tenant (ARCH §4)."""

    rules: list[PreclassRule] = Field(default_factory=list)

    def classify(self, evt: object) -> str | None:
        """The first rule whose ``when`` all match wins; else ``None`` (abstain)."""
        for rule in self.rules:
            if _rule_matches(rule.when, evt):
                return rule.label
        return None


def _rule_matches(when: Mapping[str, str], evt: object) -> bool:
    for field, pattern in when.items():
        value = getattr(evt, field, None)
        if value is None:
            return False
        if re.search(pattern, str(value)) is None:
            return False
    return True
