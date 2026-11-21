"""Detector review and rule files on disk (WP-215; the YAML side of ``domain.signal_detectors``)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path

import yaml

from supportagent.domain.signal_detectors import (
    Engine,
    NotProposed,
    SignalDetector,
    enabled_ids,
    review_document,
    rules_document,
)

REVIEW_FILE = "detectors.proposed.yaml"
#: The drafts exactly as measured: ``--apply`` renders these, so what a person
#: reviewed is what loads (a later measurement can't move a threshold).
DRAFTS_FILE = "detectors.json"
PROMETHEUS_RULES = "supportagent-detectors.yml"
LOKI_RULES = "loki/supportagent-detectors.yml"

_REVIEW_HEADER = (
    "# Detectors onboarding proposes (ADR-0022 §3, WP-215). Review each one, set\n"
    "# `enabled: true` on those you want, then `supportagent onboard detectors --apply`.\n"
    "# Only `enabled` is read back: each rule is re-derived from its measurement, so\n"
    "# editing `expr` here has no effect.\n"
)
_RULES_HEADER = (
    "# Written by `supportagent onboard detectors --apply` (WP-215) from the\n"
    "# detectors a person enabled. Edit the review file and re-apply, not this file.\n"
)


def _dump(doc: object) -> str:
    return yaml.safe_dump(doc, sort_keys=False, width=200)


def write_review(
    workspace: Path,
    detectors: Sequence[SignalDetector],
    skipped: Sequence[NotProposed],
    *,
    system: str,
) -> Path:
    """Write the review file, keeping what a person already enabled."""
    path = workspace / REVIEW_FILE
    kept = read_enabled(workspace)
    marked = [d if d.id not in kept else _enable(d) for d in detectors]
    path.write_text(_REVIEW_HEADER + _dump(review_document(marked, skipped, system)), "utf-8")
    drafts = [{**asdict(d), "measured": dict(d.measured), "enabled": False} for d in detectors]
    (workspace / DRAFTS_FILE).write_text(json.dumps(drafts, indent=1, sort_keys=True), "utf-8")
    return path


def read_drafts(workspace: Path) -> list[SignalDetector]:
    """The drafts ``onboard detectors`` measured; none before it has run."""
    path = workspace / DRAFTS_FILE
    if not path.is_file():
        return []
    raw = json.loads(path.read_text("utf-8"))
    return [SignalDetector(**item) for item in raw if isinstance(item, dict)]


def _enable(det: SignalDetector) -> SignalDetector:
    return replace(det, enabled=True)


def read_enabled(workspace: Path) -> frozenset[str]:
    path = workspace / REVIEW_FILE
    if not path.is_file():
        return frozenset()
    try:
        return enabled_ids(yaml.safe_load(path.read_text("utf-8")))
    except yaml.YAMLError:
        return frozenset()


def write_rules(rules_dir: Path, detectors: Sequence[SignalDetector]) -> list[Path]:
    """The enabled detectors as one Prometheus and one Loki rule file."""
    written = []
    targets: tuple[tuple[Engine, str], ...] = (
        ("prometheus", PROMETHEUS_RULES),
        ("loki", LOKI_RULES),
    )
    for engine, name in targets:
        path = rules_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_RULES_HEADER + _dump(rules_document(detectors, engine)), "utf-8")
        written.append(path)
    return written


__all__ = [
    "DRAFTS_FILE",
    "LOKI_RULES",
    "PROMETHEUS_RULES",
    "REVIEW_FILE",
    "read_drafts",
    "read_enabled",
    "write_review",
    "write_rules",
]
