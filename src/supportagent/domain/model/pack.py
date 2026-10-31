"""Skill pack manifests (ADR-0011 §1, WP-M01).

A pack is ``packs/<id>/pack.yaml``: one technology stack's failure knowledge.
File references are relative to the pack's directory and every one is
optional, so a manifest can grow from a stub to a full pack. The ``common``
pack has no manifest: it is built from the tenant's ``paths.taxonomy`` /
``paths.hypotheses`` / ``paths.runbooks`` and is always active.
"""

from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from supportagent.domain.model.base import DomainModel
from supportagent.domain.slots import SlotName

_PACK_ID = r"^[a-z0-9][a-z0-9-]{1,40}$"
_SEMVER = r"^\d+\.\d+\.\d+$"
_STACK_TAG = r"^[a-z0-9][a-z0-9.+-]{0,40}$"
#: The pack that every case gets; reserved, it has no manifest.
COMMON_PACK = "common"


class AppliesTo(DomainModel):
    """Which services select the pack: any shared ``stack`` tag (catalog ``stack:``)."""

    stack: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _tags(self) -> AppliesTo:
        for tag in self.stack:
            if not re.fullmatch(_STACK_TAG, tag):
                raise ValueError(f"bad stack tag {tag!r}")  # noqa: TRY003
        return self


class KnowledgeScope(DomainModel):
    """Reserved (ADR-0011 §4, WP-M08): which doc spaces and labels the pack reads."""

    spaces: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class PackManifest(DomainModel):
    """One ``packs/<id>/pack.yaml`` (ADR-0011 §1)."""

    id: str = Field(pattern=_PACK_ID)
    version: str = Field(pattern=_SEMVER)
    description: str = Field(min_length=1)
    applies_to: AppliesTo = Field(default_factory=AppliesTo)
    #: A placeholder loads and is reported, but is never selected (ADR-0011 §1).
    placeholder: bool = False
    taxonomy: str | None = None
    hypotheses: str | None = None
    preclass: str | None = None
    runbooks: str | None = None
    probes: str | None = None
    #: WP-O6a: when several stack packs' runbooks match a case, their steps run
    #: lowest first. A database pack goes before an application pack: a lock
    #: wait explains the pool timeouts it causes, not the other way round.
    plan_order: int = Field(default=50, ge=0, le=100)
    #: Tool capabilities the pack uses; an unknown one is reported as unbound.
    tools: list[str] = Field(default_factory=list)
    prompts: dict[SlotName, str] = Field(default_factory=dict)
    knowledge: KnowledgeScope | None = None
    evals: str | None = None

    @field_validator("prompts", mode="before")
    @classmethod
    def _slot_keys(cls, value: object) -> object:
        # Strict models don't coerce str keys to the enum; map slot names here.
        if isinstance(value, dict):
            return {SlotName(k) if isinstance(k, str) else k: v for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def _shape(self) -> PackManifest:
        if self.id == COMMON_PACK:
            raise ValueError(  # noqa: TRY003
                "'common' is reserved: it is built from paths.taxonomy/hypotheses/runbooks"
            )
        if not self.placeholder and not self.applies_to.stack:
            raise ValueError(  # noqa: TRY003
                f"pack {self.id!r}: applies_to.stack is empty, so no service could select it"
            )
        return self


__all__ = ["COMMON_PACK", "AppliesTo", "KnowledgeScope", "PackManifest"]
