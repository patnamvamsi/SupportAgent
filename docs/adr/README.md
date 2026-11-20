# Architecture Decision Records

The spec in [../spec/ARCHITECTURE.md](../spec/ARCHITECTURE.md) is frozen for the v0.0 build. From here on, any change to it is an ADR in this folder, attached to the code PR that implements the change. The PR updates the spec text and adds the ADR in the same commit.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-python-package-layout.md) | Python package layout: hexagonal `src/supportagent/` | accepted |
| [0002](0002-calibrated-score-definition.md) | `calibrated_score` is the Wilson lower bound of per-label holdout accuracy | accepted |
| 0003 | Provisional thresholds from the v0.0 shadow run | expected at the end of v0.0 (WP-016) |
| [0004](0004-yaml-parsing-dependency.md) | PyYAML as the explicit runtime dependency for config parsing | accepted |
| [0005](0005-t1-grant-scope-per-service.md) | T1 actions are scoped to the catalog service | accepted |
| [0006](0006-no-paging-integration-safe-default.md) | A service without a paging integration safe-defaults as `no-paging-integration` | accepted |
| [0007](0007-storm-member-safe-default.md) | A storm group's non-lead members safe-default as `storm-member` | accepted |
| [0008](0008-assisted-mode-in-v0-1.md) | Assisted mode ships in v0.1 | accepted |
| [0009](0009-severity-from-source-priority.md) | Severity comes from the source priority | accepted |
| [0010](0010-creation-values-are-human-set.md) | A human creator's values at creation are human-set | accepted |
| [0011](0011-modular-framework.md) | A modular framework: skill packs, tool bindings, connectors, knowledge, evals that gate them, analyst-authored extensions | accepted (direction) |
| [0012](0012-mcp-client-on-httpx.md) | The MCP client is a small synchronous client on httpx; the SDK is the reference server in tests | accepted |
| [0013](0013-connector-seam.md) | The connector seam: neutral ports, stored keys kept, shadow-only degradation | accepted |
| [0014](0014-sop-driven-requests.md) | SOP-driven requests: SOPs propose existing action kinds; auto mode is a conditioned grant | accepted |
| [0015](0015-system-onboarding-and-plugins.md) | System onboarding: an instance learns the system it supports, asks for what it lacks, and proves readiness; everything pluggable is a plugin | accepted |
| [0016](0016-plugins-are-data.md) | Plugins are data: declared capabilities and declarative extractors, so a new stack needs no core code | accepted |
| [0017](0017-onboarding-autopilot.md) | Onboarding autopilot: any system, one command; install what exists, name what doesn't, resume when it arrives | accepted |
| [0018](0018-live-evidence-capture.md) | Live evidence capture: replay a real system's history from its own tools, as of each incident | accepted |
| [0019](0019-credential-binding.md) | Credential binding: a credential is stored only once it is proven read-only | accepted |
| [0020](0020-shadow-replay.md) | Shadow replay: readiness also asks whether the pipeline would route it | accepted |
| [0021](0021-transaction-tracing.md) | Transaction tracing: where is this business object, and is that where it should be? | accepted |
| [0022](0022-v0-2-sre-on-live-signals.md) | v0.2 re-scoped: SRE on live signals, proven on a real system that the product discovers | accepted (§6 acting on approval: go-ahead 2026-11-21) |

## Naming

`NNNN-short-title.md`, numbered in order, for example `0001-provisional-thresholds-from-v0-shadow.md`. Copy [0000-template.md](0000-template.md) to start.

## Status values

`proposed` → `accepted` | `rejected`; an accepted ADR may later be `superseded by NNNN`.
