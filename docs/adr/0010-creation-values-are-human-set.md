# 0010. A human creator's values at creation are human-set

- **Status:** accepted
- **Date:** 2026-10-25
- **Code PR:** BL-048 (comms cadence on a new ServiceNow ticket)
- **Spec sections changed:** ARCHITECTURE.md §6.3 (auto-send rule); CAPABILITIES.md §4 (template safety); 02-interfaces.md §6 (`TicketSource.record`)

## Context

A customer comms plan's `record_state` fields (`state`, `priority`) may auto-send only a value **a human actor last set** (ARCH §6.3, §8.2). The cadence read that from the record's `sys_audit` history.

The staging exit demo on a real developer instance (INC0010004, 2026-10-25) showed that this rule can't be met on an active ServiceNow ticket:

- ServiceNow audits **changes**, not the insert. A new incident has no audit row for the `state` and `priority` it was created with, so the first slot was skipped ("no human-set value").
- A human then **changing** `state` is a takeover (ARCH §8.2). The case becomes `human_owned`, and the cadence only drafts for the owner.

So an approved plan could never auto-send while the case was active, only the final "resolved" update. The fake histories in the tests always held a human change, which hid this.

## Decision

- When the record was created by a **human actor** (`sys_created_by` is not the integration user, `system`/`guest`, or a confirmed automation actor; ARCH §8.2), each `record_state` field's **value at creation is human-set**.
- The value at creation is the `oldvalue` of that field's first audited change, or the record's current value when the field was never changed. An empty value yields nothing.
- Later changes by a human override it, as before. Changes by the agent or by automation never count.
- `TicketSource` gains `record(table, sys_id)`, which returns the record's current fields including `sys_created_by` (02 §6).

## Consequences

- An approved plan on a ticket a person raised now sends its first update without waiting for a human change, which a takeover would have turned into a draft anyway.
- A record the integration user or a confirmed automation created still needs a human change before any update sends, so monitoring-raised incidents keep the stricter rule.
- The agent still never asserts a status: a creation value repeats what the person who raised the ticket set.
- One more ServiceNow read per due plan and slot (`GET /api/now/table/<table>/<sys_id>`).
- Tests: `test_cadence_sends_the_values_a_human_set_at_creation`, `test_creation_values_do_not_count_when_the_agent_created_the_record`, `test_creation_value_is_the_first_changes_old_value` (tests/integration/test_wp102_comms.py); `test_record_returns_current_fields_or_none` (contract, both implementations).
