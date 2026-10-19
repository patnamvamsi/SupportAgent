"""Pre-classifier rule models (ARCH §4).

The ``DomainModel`` subclasses for the ordered ``when``/``label``
pre-classifier. Re-exported by :mod:`supportagent.domain.preclass` to preserve
that public import path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import Field

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.base import DomainModel

_MIN_RULE_FIELDS = 1
_SEMVER = r"^\d+\.\d+\.\d+$"


class PreclassRule(DomainModel):
    """One ordered rule: every ``when`` field/regex must match, then ``label``.

    ``when`` maps an ``AlertEvent`` field name to a regular expression matched
    with :func:`re.search`. A field whose value is absent (``None``) never
    matches. Rules are evaluated in list order; the first to match wins.
    """

    when: dict[str, str] = Field(min_length=_MIN_RULE_FIELDS)
    label: str = Field(min_length=1)

    def _assert_domain_rules(self) -> None:
        # Fail at load, not mid-triage: a typo'd field would silently never
        # match, and a bad regex would raise inside ``classify``.
        for field, pattern in self.when.items():
            if field not in AlertEvent.model_fields:
                # 01 §7.1: the message is the pinned validation API surface.
                raise DomainValidationError(  # noqa: TRY003
                    f"preclass rule {self.label!r}: unknown AlertEvent field {field!r}"
                )
            try:
                re.compile(pattern)
            except re.error as exc:
                raise DomainValidationError(  # noqa: TRY003
                    f"preclass rule {self.label!r}: invalid regex for {field!r}: {exc}"
                ) from exc


class RuleSet(DomainModel):
    """The ordered pre-classifier rules for a tenant (ARCH §4)."""

    #: Taxonomy file version (02 §1: ``taxonomy/*.yaml`` carries ``version``).
    version: str | None = Field(default=None, pattern=_SEMVER)
    rules: list[PreclassRule] = Field(default_factory=list)

    def classify(self, evt: AlertEvent) -> str | None:
        """The first rule whose ``when`` all match wins; else ``None`` (abstain)."""
        for rule in self.rules:
            if _rule_matches(rule.when, evt):
                return rule.label
        return None


def _rule_matches(when: Mapping[str, str], evt: AlertEvent) -> bool:
    for field, pattern in when.items():
        value = getattr(evt, field, None)
        if value is None:
            return False
        if re.search(pattern, str(value)) is None:
            return False
    return True
