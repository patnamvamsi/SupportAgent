# 0005. T1 actions are scoped to the catalog service

- **Status:** accepted
- **Date:** 2026-10-20
- **Code PR:** WP-012 (identity and first-run setup)
- **Spec sections changed:** 10-wp-v0.0.md WP-011 (proposal scope), ARCHITECTURE.md §6.4 (precision added; behaviour as intended)

## Context

ARCH §6.4 says "T1 policy-allowed by default": first-run setup writes `requested` grants for the default T1 kinds (`work_note`, `enrich_page`, `watch_list_add_internal`), **scoped to the catalog's services**, and the first admin approves them in one batch. WP-011, however, gave work-note proposals a per-record scope (`servicenow:incident:<sys_id>:work_note`). The gate trigger compares scopes with `=`, so a grant written at setup, before any incident exists, could never match a proposal. The setup screen would have authorized nothing, and every T1 action would have waited for a human.

## Decision

T1 kinds use the literal scope **`service:<name>:<kind>`**, where `<name>` is the catalog service the case resolved to. Every other kind keeps a per-target literal scope (`servicenow:<table>:<sys_id>:<kind>`, `airflow:dag:<dag>`, …). Both shapes are built in one place, `domain/scopes.py`, which both `app/propose.py` and `app/setup.py` call.

What does not change:

- Matching stays exact (`=`); there is still no pattern matching in the enforcement path.
- The action's `TargetRef` still names the concrete record, and the catalog check (ARCH §7) still binds the target to the case's service, so neither the LLM nor a ticket body can point a T1 action at another service's record.
- Customer-facing kinds are never grant-authorized (trigger, §6.2), whatever their scope.

## Consequences

- An approved setup grant now authorizes that T1 kind on every case of that service, which is what the setup screen promises. `test_triage_work_note_auto_approved_by_setup_grant` and the WP-011 DoD test use `service:payments:work_note`.
- A grant is coarser: revoking it removes that T1 automation for the whole service, not for one record. That is the intended granularity for internal, low-risk actions. T2 kinds stay per-target.
- A new catalog service needs its own grant: setup requests one per (service × T1 kind).
