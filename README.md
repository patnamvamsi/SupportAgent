# SupportAgent

A deployable, config-driven application support analyst. It triages ServiceNow incidents and alerts, runs bounded root-cause checks, drafts communications, and proposes actions. State-changing and customer-facing actions execute only through a database-enforced approval path.

**Status:** design phase. The architecture spec is **frozen for the v0.0 build** after three review rounds. There is no code yet. Further spec changes go through [ADRs](docs/adr/) attached to the code PRs that implement them.

## Start here

| If you want to… | Read |
|---|---|
| Understand the system visually in ten minutes | [docs/visual/blueprint.html](docs/visual/blueprint.html) (open in a browser) |
| Know the guarantees, contracts and schema (normative) | [docs/spec/ARCHITECTURE.md](docs/spec/ARCHITECTURE.md) |
| See the capability registry, tiers and release map | [docs/spec/CAPABILITIES.md](docs/spec/CAPABILITIES.md) |
| See phases, exit criteria, risks and metrics | [docs/spec/ROADMAP.md](docs/spec/ROADMAP.md) |
| Understand why it wins (positioning, demo story) | [docs/PITCH.md](docs/PITCH.md) |
| See what changed in review, and why | [docs/CHANGELOG.md](docs/CHANGELOG.md) |
| Read the design reviews themselves | [docs/reviews/](docs/reviews/) |
| **Build it** (contracts, schema, tests, standards, work packages) | [docs/tech-spec/](docs/tech-spec/README.md) |

Where documents disagree, **ARCHITECTURE.md wins**.

## Repository layout

```
SupportAgent/
├── README.md                    ← you are here
└── docs/
    ├── spec/                    normative design (frozen for v0.0)
    │   ├── ARCHITECTURE.md      invariants, contracts, data model, components
    │   ├── CAPABILITIES.md      capability registry C1–C22, tier model, release map
    │   └── ROADMAP.md           phases, exit criteria, risks, success metrics
    ├── tech-spec/               how to build it: contracts, DDL, tests, standards, versioning,
    │                            and work packages per release (WP-0xx … WP-3xx)
    ├── adr/                     post-freeze decisions, one per code PR that changes the spec
    ├── reviews/                 adversarial design reviews, in order
    │   ├── critique-v1.md
    │   ├── critique-v2.md
    │   └── critique-v3.md
    ├── visual/
    │   └── blueprint.html       visual guide: trust core, plug-in layers, RCA, packs, evals, status
    ├── CHANGELOG.md             disposition of every review finding
    └── PITCH.md                 positioning and sales narrative
```

The code layout (`src/supportagent/` split into domain / ports / app / adapters / entrypoints, plus the data directories `migrations/`, `prompts/`, `taxonomy/`, `catalog/`, `runbooks/` and `evals/`) is specified in [tech-spec 01 §1](docs/tech-spec/01-architecture-and-contracts.md#1-package-layout), per [ADR-0001](docs/adr/0001-python-package-layout.md). It will sit beside `docs/` at the repository root.

## Next step

Start at [WP-000](docs/tech-spec/10-wp-v0.0.md#wp-000-repository-tooling-ci) in the v0.0 work packages. In week 1, commit every test ID listed in [ARCHITECTURE.md §19](docs/spec/ARCHITECTURE.md#19-ci-gates-measured-not-promised) as a failing test, and send the design-partner data-access request.
