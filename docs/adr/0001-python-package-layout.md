# 0001. Python package layout: hexagonal `src/supportagent/`

- **Status:** accepted
- **Date:** 2026-10-17
- **Code PR:** WP-000 (repository bootstrap)
- **Spec sections changed:** ARCHITECTURE.md §18 (the layout becomes a mapping; the components are unchanged)

## Context

ARCH §18 sketches top-level folders named `core/`, `kb/`, `api/` and `cli/`. Those are not importable as one distribution, and `core` is a name that collides easily. The spec also needs enforceable layer rules, so that INV-01..06 logic can be tested without I/O.

## Decision

All code lives in one package, `src/supportagent/`, split into `domain`, `ports`, `app`, `adapters`, `config`, `evals`, `entrypoints` and `bootstrap` (tech-spec 01 §1). Import-linter contracts enforce the layers (tech-spec 01 §2). ARCH §18 components map as follows: `core/pipeline.py` → `app/triage.py`; `core/gate.py` → `app/gate.py` plus `adapters/db` triggers; `core/llm.py` → `adapters/llm/client.py`; `core/redact.py` → `adapters/llm/redact.py`; `core/dedup.py` → `domain/fingerprint.py` plus `CaseRepository.upsert_by_fingerprint`; `kb/*` → `app/memory.py`, `app/rca.py`, `domain/rca_reduce.py`; `api/` → `entrypoints/api`; `cli/` → `entrypoints/cli`. The data directories (`taxonomy/`, `catalog/`, `runbooks/`, `prompts/`, `evals/cases`) stay at the repository root.

## Consequences

The domain rules become pure functions with millisecond tests. Adapters can be swapped (SQLite and Postgres, LLM providers, fake ServiceNow). Every PR pays the cost of honouring import-linter, which is the intent.
