# 0013. The connector seam: neutral ports, stored keys kept, shadow-only degradation

- **Status:** accepted
- **Date:** 2026-10-27
- **Code PR:** WP-M07 (connector seam)
- **Spec sections changed:** 01-architecture-and-contracts §3 (`ports/tickets.py`); ADR-0011 §3 (two refinements, below)

## Context

ADR-0011 §3 made the incident system a plug-in: the ticket ports become a connector contract, ServiceNow is refactored onto it with no behaviour change, and a connector missing a capability degrades safely. Building it raised three questions that ADR-0011 left open:

- **Stored values.** `SnTable` values (`incident`, `em_alert`) are stored in sync cursors, `cases.external_table`, and action scopes (`servicenow:incident:<id>:…`). Journal field names show up in outbox `external_ref`s (`<id>#work_notes`). Renaming the values would need a data migration and would change scopes that are already granted.
- **Normalized changes.** ADR-0011 proposed `assignee_changed` / `status_changed` change kinds. Normalizing field names at the adapter would also rewrite the field diffs that feedback and comms read (`record_state` fields such as `state` and `priority` are named in comms plans, and plan hashes cover them).
- **What "degrade safely" allows.** ADR-0011 says a connector without history needs human approval for every write. In assisted mode, though, grant-covered work notes and approved comms cadences send without a per-action approval. That makes "every write approved" a new mode, not just a configuration of an existing one.

## Decision

- **Neutral names in the core, kept values.**
  - The ports use `RecordType` (`INCIDENT="incident"`, `ALERT="em_alert"`), `NoteVisibility` (`INTERNAL`, `CUSTOMER`) and `external_id`.
  - `TicketRecord` and `ChangeRecord` use `external_id`, `revision`, `updated_at`, `updated_by`, `created_by` and `alert_source`.
  - The enum *values* are the stored keys and stay as they were in v0.0, so nothing is migrated.
  - The ServiceNow sink maps `NoteVisibility` to `work_notes` / `comments`, so refs are unchanged.
  - `cmdb_ci` stays: a CMDB configuration item is the ITIL concept, it's in catalogs and `AlertEvent` (which is serialized), and other tools use the term too.
- **The connector supplies semantics, not normalized field names.** `domain.connector.TicketSemantics` names the state, assignee and priority fields and provides:
  - the resolved states (BL-032 config still overrides them);
  - priority to severity (ADR-0009);
  - priority escalation (BL-047);
  - customer display values.

  The takeover rules stay in the core and read the field names from it. `classify_actor` is the classified actor (`agent`, `system`, `automation`, `human`).
- **A capability manifest, with stricter degradation than ADR-0011.** `ConnectorManifest` lists `poll`, `record`, `history`, `internal_notes`, `customer_notes`, `field_updates` and `marker_read`; `limits_for` derives what is allowed:
  - Without `history` the sync service delivers no changes for known records, so there is no takeover detection.
  - Without `marker_read` nothing is written, because sends can't be reconciled (INV-04).
  - Either one limits the deployment to **shadow** mode: the worker refuses to start otherwise, and the `connector` doctor check fails.
  - A per-action-approval-only mode for such connectors is BL-053.
  - Comms plans need `customer_notes`.
- **Errors.** Core code catches `domain.errors.ConnectorError`, which carries a `status`. `ServiceNowError` subclasses it in `adapters/servicenow/errors.py`.
- **Doctor.** Connector-specific checks live in `app/doctor/connectors/<name>.py` and register a `ConnectorDoctor`, which holds the checks plus automation-actor discovery. The default registry runs them after `db`, followed by the generic `connector` check. The probe port is `RecordProbe`.
- **Enforcement.** The import-linter contract `core-no-servicenow` forbids the core from importing the ServiceNow adapters. `test_core_has_no_servicenow_types` scans core code identifiers for vendor names, excluding `app/doctor/connectors/`.

## Consequences

- **Better:**
  - a second connector touches the adapter, its semantics, and one doctor module, never triage, comms, sync, takeover or feedback;
  - no data migration;
  - behaviour is pinned by the unchanged contract suites (fake and real adapter) and `test_servicenow_connector_contract_unchanged`.
- **Worse:**
  - The stored value `em_alert` is a ServiceNow table name living on as a neutral enum's value. A future connector maps its alert record type onto it.
  - A connector without history runs shadow-only until BL-053.
  - The `servicenow:` prefix in action scopes and the `servicenow_comment` comms channel stay as values: they are configuration and hashed into plans.
- **Tests:**
  - `test_core_has_no_servicenow_types`;
  - `test_servicenow_connector_contract_unchanged`;
  - `test_missing_history_capability_disables_auto_takeover`;
  - `test_missing_marker_read_disables_writes`;
  - `test_connector_doctor_checks_registered`;
  - `test_actor_classification`.
- **Watch:**
  - the first customer-driven second connector (Jira Service Management, Zendesk, PagerDuty);
  - whether its record types fit `RecordType`, or need a third value and a migration.
