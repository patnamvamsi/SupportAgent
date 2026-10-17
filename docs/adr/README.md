# Architecture Decision Records

The spec in [../spec/ARCHITECTURE.md](../spec/ARCHITECTURE.md) is frozen for the v0.0 build. From here on, any change to it is an ADR in this folder, attached to the code PR that implements the change. The PR updates the spec text and adds the ADR in the same commit.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-python-package-layout.md) | Python package layout: hexagonal `src/supportagent/` | accepted |
| [0002](0002-calibrated-score-definition.md) | `calibrated_score` is the Wilson lower bound of per-label holdout accuracy | accepted |
| 0003 | Provisional thresholds from the v0.0 shadow run | expected at the end of v0.0 (WP-016) |

## Naming

`NNNN-short-title.md`, numbered in order, for example `0001-provisional-thresholds-from-v0-shadow.md`. Copy [0000-template.md](0000-template.md) to start.

## Status values

`proposed` → `accepted` | `rejected`; an accepted ADR may later be `superseded by NNNN`.
