"""Cross-file bundle validation (02 §1, ARCH §12): config + catalog + taxonomies.

``validate_bundle`` reports **every** issue it finds (not just the first): a
doctor-style output needs the full list (02 §1). The catalog and hypothesis
taxonomies are already self-validated at construction (01 §4: unique names,
known ``depends_on`` / ``excludes`` references); the cross-file rule this
function owns is that routing labels exist in the classification taxonomy and
no routing row proposes a never-executable kind (01 §4).
"""

from __future__ import annotations

from dataclasses import dataclass

from supportagent.config.schema import AppConfig, RoutingRow
from supportagent.domain.kinds import NEVER_EXECUTABLE_KINDS
from supportagent.domain.model import Catalog, ClassificationTaxonomy, HypothesisTaxonomy


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """One bundle issue: a code, a human-readable message, and a stable path."""

    code: str
    message: str
    path: str


def validate_bundle(
    cfg: AppConfig,
    catalog: Catalog,
    taxonomy: ClassificationTaxonomy,
    hypotheses: HypothesisTaxonomy,
) -> list[ConfigIssue]:
    """All bundle issues, or an empty list when the bundle validates cleanly."""
    known_labels = {label.id for label in taxonomy.labels}
    issues: list[ConfigIssue] = []
    for index, row in enumerate(cfg.routing):
        _row_issues(row, f"routing[{index}]", known_labels, issues)
    if cfg.default is not None:
        _row_issues(cfg.default, "default", known_labels, issues)
    # `catalog` and `hypotheses` are consumed by the doctor (ARCH §12) once it
    # exists (WP-015); their internal integrity is enforced at construction.
    del catalog, hypotheses
    return issues


def _row_issues(
    row: RoutingRow,
    path: str,
    known_labels: set[str],
    issues: list[ConfigIssue],
) -> None:
    if row.match is not None and row.match.classification not in known_labels:
        issues.append(
            ConfigIssue(
                code="routing.label-unknown",
                message=(
                    f"routing label '{row.match.classification}' is not defined "
                    "in the classification taxonomy"
                ),
                path=f"{path}.match.classification",
            )
        )
    for kind in row.actions:
        if kind in NEVER_EXECUTABLE_KINDS:
            issues.append(
                ConfigIssue(
                    code="routing.kind-never-executable",
                    message=(
                        f"action '{kind.value}' is never executable and must not appear in routing"
                    ),
                    path=f"{path}.actions",
                )
            )
