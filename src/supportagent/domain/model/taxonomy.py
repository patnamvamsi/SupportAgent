"""Classification and hypothesis taxonomies (ARCH §12, 06 §3).

Label ids are ``<domain>/<signal>`` (01 §4 ``TriageResult``); taxonomy versions
are semver — adding a label is MINOR, rename/remove MAJOR, description PATCH
(06 §3). Hypothesis ids are snake_case and ``excludes`` names the mutually
exclusive set fed to ``reduce()`` (01 §7.1).
"""

from __future__ import annotations

from pydantic import Field, model_validator

from supportagent.domain.model.base import DomainModel

_LABEL_ID = r"^[a-z0-9_]+/[a-z0-9_]+$"
_HYPOTHESIS_ID = r"^[a-z0-9_]+$"
_SEMVER = r"^\d+\.\d+\.\d+$"


class ClassificationLabel(DomainModel):
    """One classification label (01 §4 ``TriageResult.classification``)."""

    id: str = Field(pattern=_LABEL_ID)
    description: str = Field(min_length=1)


class ClassificationTaxonomy(DomainModel):
    """Whole ``taxonomy/classification.yaml`` file (ARCH §12, 06 §3)."""

    version: str = Field(pattern=_SEMVER)
    labels: list[ClassificationLabel] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_label_ids(self) -> ClassificationTaxonomy:
        ids = [label.id for label in self.labels]
        if len(ids) != len(set(ids)):
            raise ValueError("label ids must be unique")  # noqa: TRY003
        return self


class Hypothesis(DomainModel):
    """One hypothesis seed (ARCH §12.1); ``excludes`` is the conflict set."""

    id: str = Field(pattern=_HYPOTHESIS_ID)
    excludes: list[str] = Field(default_factory=list)


class HypothesisTaxonomy(DomainModel):
    """Whole ``taxonomy/hypotheses.yaml`` file (ARCH §12.1)."""

    version: str = Field(pattern=_SEMVER)
    hypotheses: list[Hypothesis] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids_and_known_excludes(self) -> HypothesisTaxonomy:
        ids = [h.id for h in self.hypotheses]
        if len(ids) != len(set(ids)):
            raise ValueError("hypothesis ids must be unique")  # noqa: TRY003
        known = set(ids)
        for hypothesis in self.hypotheses:
            for excluded in hypothesis.excludes:
                if excluded not in known:
                    raise ValueError(  # noqa: TRY003
                        f"excludes references unknown hypothesis id '{excluded}'"
                    )
        return self
