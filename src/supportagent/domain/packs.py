"""Skill packs, loaded and selected per case (ADR-0011 §1, WP-M01).

A :class:`Pack` is a manifest plus its loaded knowledge. A :class:`PackSet` is
every pack the tenant ships, validated together: label and hypothesis ids are
unique across packs, and every pre-classifier label names a label some pack
defines. Per case, :meth:`PackSet.active` picks ``common`` plus each
non-placeholder pack whose ``applies_to.stack`` shares a tag with the
service's catalog ``stack:``. Only those packs' labels, rules and hypotheses
reach the classifier and the RCA planner.

Pure domain code: the loader lives in ``adapters/files/pack_loader.py``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from supportagent.domain.errors import ConfigError
from supportagent.domain.model.pack import COMMON_PACK
from supportagent.domain.model.preclass import PreclassRule, RuleSet
from supportagent.domain.model.taxonomy import (
    ClassificationLabel,
    ClassificationTaxonomy,
    Hypothesis,
    HypothesisTaxonomy,
)
from supportagent.domain.slots import SlotName

#: Characters a pack's prompt fragment may add to a slot prompt (ADR-0011 §1).
FRAGMENT_BUDGET_CHARS = 2000


@dataclass(frozen=True, slots=True)
class Pack:
    """One pack's identity and loaded knowledge."""

    id: str
    version: str
    stack: frozenset[str]
    labels: tuple[ClassificationLabel, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    rules: tuple[PreclassRule, ...] = ()
    #: Runbook ids this pack ships (the runbooks themselves load with RCA).
    runbook_ids: tuple[str, ...] = ()
    #: Where RCA loads this pack's runbooks and probes (compiled against tool schemas).
    runbooks_dir: str | None = None
    probes_path: str | None = None
    tools: tuple[str, ...] = ()
    fragments: Mapping[SlotName, str] = field(default_factory=dict)
    placeholder: bool = False
    #: Where the pack's lab scenarios live (WP-M02); ``None``: not evaluated.
    evals: str | None = None

    @property
    def always(self) -> bool:
        return self.id == COMMON_PACK


@dataclass(frozen=True, slots=True)
class PackSet:
    """Every pack the tenant ships, validated together."""

    packs: tuple[Pack, ...]
    #: Versions of the ``common`` files, for the merged views.
    taxonomy_version: str
    hypotheses_version: str
    rules_version: str | None = None

    def __post_init__(self) -> None:
        ids = [p.id for p in self.packs]
        if len(ids) != len(set(ids)):
            raise ConfigError(f"duplicate pack ids: {sorted(ids)}")  # noqa: TRY003
        if COMMON_PACK not in ids:
            raise ConfigError("the common pack is missing")  # noqa: TRY003
        _unique("label", [(p.id, label.id) for p in self.packs for label in p.labels])
        _unique("hypothesis", [(p.id, h.id) for p in self.packs for h in p.hypotheses])
        _unique("runbook", [(p.id, r) for p in self.packs for r in p.runbook_ids])
        labels = {label.id for p in self.packs for label in p.labels}
        hypotheses = {h.id for p in self.packs for h in p.hypotheses}
        for pack in self.packs:
            for rule in pack.rules:
                if rule.label not in labels:
                    raise ConfigError(  # noqa: TRY003
                        f"pack {pack.id!r}: preclass rule label {rule.label!r} is not in any pack"
                    )
            for hypothesis in pack.hypotheses:
                unknown = set(hypothesis.excludes) - hypotheses
                if unknown:
                    raise ConfigError(  # noqa: TRY003
                        f"pack {pack.id!r}: {hypothesis.id} excludes unknown {sorted(unknown)}"
                    )
            for slot, text in pack.fragments.items():
                if len(text) > FRAGMENT_BUDGET_CHARS:
                    raise ConfigError(  # noqa: TRY003
                        f"pack {pack.id!r}: {slot.value} fragment is {len(text)} chars "
                        f"(budget {FRAGMENT_BUDGET_CHARS})"
                    )

    # --- selection -------------------------------------------------------------

    def active(self, stack: Collection[str]) -> tuple[Pack, ...]:
        """``common`` plus every non-placeholder pack sharing a stack tag, in pack order."""
        tags = set(stack)
        return tuple(p for p in self.packs if p.always or (not p.placeholder and p.stack & tags))

    def get(self, pack_id: str) -> Pack:
        for pack in self.packs:
            if pack.id == pack_id:
                return pack
        raise KeyError(pack_id)

    # --- views -----------------------------------------------------------------

    def taxonomy(self, packs: Sequence[Pack] | None = None) -> ClassificationTaxonomy:
        chosen = self.packs if packs is None else packs
        return ClassificationTaxonomy(
            version=self.taxonomy_version,
            labels=[label for p in chosen for label in p.labels],
        )

    def hypothesis_taxonomy(self) -> HypothesisTaxonomy:
        return HypothesisTaxonomy(
            version=self.hypotheses_version,
            hypotheses=[h for p in self.packs for h in p.hypotheses],
        )

    def rule_set(self, packs: Sequence[Pack] | None = None) -> RuleSet:
        chosen = self.packs if packs is None else packs
        return RuleSet(version=self.rules_version, rules=[r for p in chosen for r in p.rules])

    def hypothesis_ids(self, packs: Sequence[Pack]) -> frozenset[str]:
        return frozenset(h.id for p in packs for h in p.hypotheses)

    def runbook_ids(self, packs: Sequence[Pack]) -> frozenset[str]:
        return frozenset(r for p in packs for r in p.runbook_ids)


@dataclass(frozen=True, slots=True)
class PackScope:
    """A pack set plus each catalog service's ``stack:``: the per-case view (ADR-0011)."""

    packs: PackSet
    stacks: Mapping[str, frozenset[str]]
    _rules: dict[frozenset[str], RuleSet] = field(default_factory=dict, compare=False)
    _labels: dict[frozenset[str], str] = field(default_factory=dict, compare=False)

    def active(self, service: str | None) -> tuple[Pack, ...]:
        return self.packs.active(self.stacks.get(service or "", frozenset()))

    def rule_set(self, service: str | None) -> RuleSet:
        key = self.stacks.get(service or "", frozenset())
        if key not in self._rules:
            self._rules[key] = self.packs.rule_set(self.packs.active(key))
        return self._rules[key]

    def labels_text(self, service: str | None) -> str:
        key = self.stacks.get(service or "", frozenset())
        if key not in self._labels:
            self._labels[key] = labels_text(self.packs.taxonomy(self.packs.active(key)).labels)
        return self._labels[key]


def labels_text(labels: Sequence[ClassificationLabel]) -> str:
    """The classify prompt's label list: one ``- id: description`` line each (BL-043)."""
    return "\n".join(f"- {label.id}: {' '.join(label.description.split())}" for label in labels)


def _unique(kind: str, owned: Sequence[tuple[str, str]]) -> None:
    seen: dict[str, str] = {}
    for pack_id, item in owned:
        if item in seen:
            raise ConfigError(  # noqa: TRY003
                f"{kind} {item!r} is defined by both {seen[item]!r} and {pack_id!r}"
            )
        seen[item] = pack_id


__all__ = ["FRAGMENT_BUDGET_CHARS", "Pack", "PackScope", "PackSet", "labels_text"]
