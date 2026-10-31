"""File adapter for skill packs (ADR-0011 §1, WP-M01).

``load_pack_set`` builds the ``common`` pack from the tenant's legacy paths
(``paths.taxonomy`` and its ``preclass_rules.yaml``, ``paths.hypotheses``,
``paths.runbooks``) and adds every ``<packs_dir>/<id>/pack.yaml``. Each
manifest's files are read with the same loaders the single-taxonomy layout
used, then :class:`~supportagent.domain.packs.PackSet` validates them
together. Runbooks are only listed here; RCA compiles them against the tool
schemas (``build_rca_knowledge``).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from supportagent.adapters.files._yaml import load_yaml_file
from supportagent.adapters.files.preclass_loader import load_preclass_rules
from supportagent.adapters.files.taxonomy_loader import load_classification, load_hypotheses
from supportagent.domain.errors import ConfigError
from supportagent.domain.model.pack import COMMON_PACK, PackManifest
from supportagent.domain.model.preclass import RuleSet
from supportagent.domain.packs import Pack, PackSet

MANIFEST = "pack.yaml"


def load_pack_set(
    *,
    taxonomy: Path,
    hypotheses: Path,
    runbooks: Path,
    packs_dir: Path | None = None,
) -> PackSet:
    """``common`` from the legacy paths, plus every manifest under ``packs_dir``."""
    classification = load_classification(taxonomy)
    hypothesis_taxonomy = load_hypotheses(hypotheses)
    rules_path = taxonomy.parent / "preclass_rules.yaml"
    rules = load_preclass_rules(rules_path) if rules_path.is_file() else RuleSet()
    common = Pack(
        id=COMMON_PACK,
        version=classification.version,
        stack=frozenset(),
        labels=tuple(classification.labels),
        hypotheses=tuple(hypothesis_taxonomy.hypotheses),
        rules=tuple(rules.rules),
        runbook_ids=_runbook_ids(runbooks),
        runbooks_dir=str(runbooks),
        probes_path=str(runbooks / "probes.yaml"),
    )
    packs = [common]
    if packs_dir is not None and packs_dir.is_dir():
        packs.extend(_load_pack(path.parent) for path in sorted(packs_dir.glob(f"*/{MANIFEST}")))
    return PackSet(
        packs=tuple(packs),
        taxonomy_version=classification.version,
        hypotheses_version=hypothesis_taxonomy.version,
        rules_version=rules.version,
    )


def _load_pack(directory: Path) -> Pack:
    path = directory / MANIFEST
    try:
        manifest = PackManifest.model_validate(load_yaml_file(path))
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc  # noqa: TRY003
    if manifest.id != directory.name:
        raise ConfigError(  # noqa: TRY003
            f"{path}: id {manifest.id!r} must match its directory {directory.name!r}"
        )
    labels = (
        load_classification(_file(directory, manifest.taxonomy)).labels if manifest.taxonomy else []
    )
    hypotheses = (
        load_hypotheses(_file(directory, manifest.hypotheses)).hypotheses
        if manifest.hypotheses
        else []
    )
    rules = (
        load_preclass_rules(_file(directory, manifest.preclass)).rules if manifest.preclass else []
    )
    runbooks = _dir(directory, manifest.runbooks) if manifest.runbooks else None
    return Pack(
        id=manifest.id,
        version=manifest.version,
        stack=frozenset(manifest.applies_to.stack),
        labels=tuple(labels),
        hypotheses=tuple(hypotheses),
        rules=tuple(rules),
        runbook_ids=_runbook_ids(runbooks) if runbooks is not None else (),
        runbooks_dir=str(runbooks) if runbooks is not None else None,
        probes_path=str(_file(directory, manifest.probes)) if manifest.probes else None,
        tools=tuple(manifest.tools),
        fragments={
            slot: _file(directory, rel).read_text(encoding="utf-8")
            for slot, rel in manifest.prompts.items()
        },
        placeholder=manifest.placeholder,
        plan_order=manifest.plan_order,
        evals=str(_dir(directory, manifest.evals)) if manifest.evals else None,
        doc_spaces=tuple(manifest.knowledge.spaces) if manifest.knowledge else (),
    )


def _file(directory: Path, rel: str) -> Path:
    path = (directory / rel).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ConfigError(f"{directory}: {rel!r} points outside the pack")  # noqa: TRY003
    if not path.is_file():
        raise ConfigError(f"{directory}: {rel!r} does not exist")  # noqa: TRY003
    return path


def _dir(directory: Path, rel: str) -> Path:
    path = (directory / rel).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ConfigError(f"{directory}: {rel!r} points outside the pack")  # noqa: TRY003
    if not path.is_dir():
        raise ConfigError(f"{directory}: {rel!r} is not a directory")  # noqa: TRY003
    return path


def _runbook_ids(directory: Path) -> tuple[str, ...]:
    if not directory.is_dir():
        return ()
    ids: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "probes.yaml":
            continue
        raw = load_yaml_file(path)
        runbook_id = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(runbook_id, str):
            raise ConfigError(f"{path}: runbook has no id")  # noqa: TRY003
        ids.append(runbook_id)
    return tuple(ids)


__all__ = ["MANIFEST", "load_pack_set"]
