# 03. Data Model and Migrations

Implements: ARCH §4.1, §4.3, §6.2, §6.3, §6.4, §8.1, §15. The gate DDL in ARCH §6.2/§6.3 is normative and is reproduced here unchanged. This file adds every remaining table, the column conventions, the Postgres twins, and the migration order.

---

## 1. Conventions

- **IDs:** `TEXT` primary keys of the form `<prefix>_<ULID>`. Prefixes: `case`, `alrt`, `prop`, `appr`, `grnt`, `obx`, `plan`, `cmms`, `evt`, `grp`, `shdw`, `fb`, `ver`. Users use their OIDC `sub` as the id.
- **Time:** `*_ts INTEGER` = Unix epoch **seconds**, UTC. `events.ts_ms INTEGER` = epoch milliseconds (ordering within a trace). No `DATETIME` columns.
- **JSON:** `TEXT` holding canonical JSON (01 §7.1) on SQLite; `JSONB` on Postgres, written as the same canonical text.
- **Tenant:** every table has `tenant_id TEXT NOT NULL`, and every index that serves a query starts with `tenant_id`.
- **Booleans:** `INTEGER` 0/1 on SQLite, `BOOLEAN` on Postgres. SQLAlchemy `Boolean` maps both.
- **Enumerations:** `TEXT` with a `CHECK (col IN (...))` generated from the domain `StrEnum`. `test_schema_enums_match_domain` fails if they drift.
- **Foreign keys:** declared everywhere. SQLite enables them per connection through an SQLAlchemy `connect` event (`PRAGMA foreign_keys=ON`; also `journal_mode=WAL` and `busy_timeout=5000`).

---

## 2. Tables

### 2.1 Identity

```sql
CREATE TABLE users (
  id TEXT PRIMARY KEY,                         -- OIDC sub
  tenant_id TEXT NOT NULL,
  email TEXT NOT NULL, name TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_ts INTEGER NOT NULL, last_login_ts INTEGER
);
CREATE TABLE role_assignments (
  user_id TEXT NOT NULL REFERENCES users(id),
  tenant_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('viewer','analyst','approver','admin')),
  scope TEXT NOT NULL DEFAULT '*',             -- '*' or 'service:<name>'
  granted_by TEXT REFERENCES users(id), granted_ts INTEGER NOT NULL,
  PRIMARY KEY (user_id, tenant_id, role, scope)
);
```
In v0.0 the first user to log in when no admin exists becomes `admin`, recorded as an event. Every later role change is made by an admin.

### 2.2 Cases, alerts, groups

```sql
CREATE TABLE case_groups (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
  opened_ts INTEGER NOT NULL, closed_ts INTEGER,
  rule TEXT NOT NULL, member_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE cases (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  origin TEXT NOT NULL CHECK (origin IN ('alert','ticket')),
  external_table TEXT, external_id TEXT, ticket_number TEXT,
  service TEXT,                                 -- catalog services.name; NULL = unresolved → safe default
  classification TEXT, severity TEXT CHECK (severity IN ('P1','P2','P3','P4')),
  status TEXT NOT NULL CHECK (status IN ('open','triaged','in_progress','human_owned','resolved','reopened')),
  decision_json TEXT,                           -- TriageDecision
  group_id TEXT REFERENCES case_groups(id),
  duplicate_count INTEGER NOT NULL DEFAULT 0,
  opened_ts INTEGER NOT NULL, last_alert_ts INTEGER NOT NULL, resolved_ts INTEGER,
  trace_id TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_cases_unresolved ON cases(tenant_id, fingerprint) WHERE status <> 'resolved';  -- INV-02
CREATE INDEX ix_cases_status ON cases(tenant_id, status, opened_ts);
CREATE INDEX ix_cases_external ON cases(tenant_id, external_table, external_id);

CREATE TABLE alerts (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
  case_id TEXT NOT NULL REFERENCES cases(id),
  adapter TEXT NOT NULL, external_id TEXT NOT NULL,
  payload_ref TEXT NOT NULL,                    -- blob key of the encrypted raw payload (§3)
  received_ts INTEGER NOT NULL
);
CREATE INDEX ix_alerts_case ON alerts(tenant_id, case_id);
```

### 2.3 Gate, outbox, comms (normative copy of ARCH §6.2 / §6.3)

`proposals`, `approvals` (+ `trg_no_self_approval`), `policy_grants`, `outbox` (+ `trg_outbox_inv01`, `trg_outbox_mark_executed`, `trg_outbox_immutable`), `comms_plans` and `comms` are exactly as in ARCH §6.2 and §6.3, with these additions:

```sql
ALTER TABLE proposals  ADD COLUMN expires_ts INTEGER;            -- created_at_ts + ttl; NULL for grant path
ALTER TABLE outbox     ADD COLUMN trace_id TEXT NOT NULL;
ALTER TABLE outbox     ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox     ADD COLUMN next_attempt_ts INTEGER;
ALTER TABLE outbox     ADD COLUMN last_error TEXT;
ALTER TABLE outbox     ADD COLUMN external_ref TEXT;            -- sys_id of the journal entry etc.
CREATE INDEX ix_proposals_pending ON proposals(tenant_id, status, expires_ts);
CREATE INDEX ix_outbox_due ON outbox(tenant_id, status, next_attempt_ts);
```
(In the actual migration these columns are part of the `CREATE TABLE`; `ALTER` is used here only to show the delta. `trg_outbox_immutable` deliberately leaves `attempts`, `next_attempt_ts`, `last_error`, `external_ref`, `status`, `claimed_at_ts` and `done_at_ts` mutable.)

The kind literals inside `trg_outbox_inv01` are **generated** by the migration from `domain.kinds.CUSTOMER_FACING_KINDS` and `NEVER_EXECUTABLE_KINDS`. Changing those sets needs a new migration that recreates the trigger. `test_trigger_kind_lists_match_code` checks this.

### 2.4 Observability

```sql
CREATE TABLE events (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
  trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
  kind TEXT NOT NULL,                           -- CHECK generated from domain EventKind
  actor TEXT NOT NULL, model TEXT,
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('ok','error','skipped')),
  payload_enc BLOB NOT NULL,                    -- encrypted canonical JSON (§3); payload has "v"
  ts_ms INTEGER NOT NULL
);
CREATE INDEX ix_events_trace ON events(tenant_id, trace_id, ts_ms);
CREATE INDEX ix_events_kind  ON events(tenant_id, kind, ts_ms);
CREATE TRIGGER trg_events_append_only BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'INV-03: events are append-only'); END;
```
DELETE is allowed only through the retention sweep, which runs through a dedicated repository method and records a `retention` summary event (row counts only).

### 2.5 Sync, leases, catalog snapshot

```sql
CREATE TABLE sync_cursors (
  tenant_id TEXT NOT NULL, table_name TEXT NOT NULL,
  cursor_ts INTEGER NOT NULL,                   -- max sys_updated_on processed
  PRIMARY KEY (tenant_id, table_name)
);
CREATE TABLE sync_seen (
  tenant_id TEXT NOT NULL, sys_id TEXT NOT NULL, sys_mod_count INTEGER NOT NULL,
  processed_ts INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, sys_id, sys_mod_count)
);
CREATE TABLE leases (
  name TEXT PRIMARY KEY, owner TEXT NOT NULL, until_ts INTEGER NOT NULL
);
-- acquire: INSERT ... ON CONFLICT(name) DO UPDATE SET owner=:me, until_ts=:t
--          WHERE leases.until_ts < :now OR leases.owner = :me   → rowcount 1 means acquired
CREATE TABLE automation_actors (                -- v0.0 table, used from v0.1
  tenant_id TEXT NOT NULL, sys_user TEXT NOT NULL, kind TEXT NOT NULL,
  confirmed_by TEXT REFERENCES users(id), confirmed_ts INTEGER,
  PRIMARY KEY (tenant_id, sys_user)
);
```
The service catalog stays in `catalog/services.yaml` (config, not DB). Each case stores the catalog version it was resolved against, in `decision_json.catalog_version`.

### 2.6 Shadow and stats

```sql
CREATE TABLE shadow_records (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
  case_id TEXT NOT NULL REFERENCES cases(id),
  alert_received_ts INTEGER NOT NULL,
  first_proposal_ts INTEGER,                    -- counterfactual "would have proposed at"
  proposed_json TEXT NOT NULL,                  -- decision + ActionSpecs that would have been submitted
  human_classification TEXT, human_severity TEXT,
  human_first_action_ts INTEGER, human_action_json TEXT,
  agreement INTEGER                             -- NULL until the human label is known
);
```

### 2.7 v0.1+ tables (created by their WPs)

```sql
-- WP-108 case memory (ARCH §5.2, §5.3)
CREATE TABLE nodes (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
                    label TEXT NOT NULL, updated_ts INTEGER NOT NULL);
CREATE TABLE edges (src TEXT NOT NULL REFERENCES nodes(id), dst TEXT NOT NULL REFERENCES nodes(id),
                    tenant_id TEXT NOT NULL, kind TEXT NOT NULL, weight INTEGER NOT NULL DEFAULT 1,
                    created_ts INTEGER NOT NULL, PRIMARY KEY (src, dst, kind));
CREATE VIRTUAL TABLE kb_fts USING fts5(tenant_id UNINDEXED, ref UNINDEXED, text);   -- SQLite
-- Postgres twin: kb_docs(tenant_id, ref, text, tsv tsvector GENERATED ...) + GIN index
-- WP-109 feedback
CREATE TABLE feedback (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, case_id TEXT NOT NULL REFERENCES cases(id),
                       kind TEXT NOT NULL CHECK (kind IN ('approval','diff','outcome')),
                       payload TEXT NOT NULL, ts INTEGER NOT NULL);
-- WP-110 RCA artifacts
CREATE TABLE rca_artifacts (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, case_id TEXT NOT NULL REFERENCES cases(id),
                            outcome TEXT NOT NULL CHECK (outcome IN ('SUPPORTED','CONSISTENT_UNCONFIRMED','INCONCLUSIVE')),
                            artifact_json TEXT NOT NULL, created_ts INTEGER NOT NULL);
-- WP-2xx learning lineage
CREATE TABLE versions (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, artifact TEXT NOT NULL, sha TEXT NOT NULL,
                       eval_report TEXT NOT NULL, holdout_disjoint_check INTEGER NOT NULL,
                       approved_by TEXT REFERENCES users(id), active INTEGER NOT NULL DEFAULT 0, ts INTEGER NOT NULL);
```

---

## 3. Payload encryption

`alerts` raw payloads and `events.payload_enc` are encrypted with **AES-256-GCM** under a per-tenant data key. The data key is wrapped by a master key from `SA_MASTER_KEY_ENV` (or a KMS adapter, v0.3). Format: `version(1 byte) | nonce(12) | ciphertext | tag(16)`. Raw alert payloads live in the `blobs` table (`key TEXT PK, tenant_id, data BLOB`). Crypto-shredding deletes the tenant's wrapped data key from `tenant_keys(tenant_id PK, wrapped_key, created_ts)`. Library: `cryptography` (AESGCM).

---

## 4. Migration sequence

Alembic revisions live in `migrations/versions/`, named `NNNN_<slug>.py`, with linear history (no branches). Each revision lists its WP and has an `upgrade()` per dialect where the SQL differs (`op.get_bind().dialect.name`).

| Rev | Slug | WP | Release | Creates |
|---|---|---|---|---|
| 0001 | base | WP-003 | v0.0 | `users`, `role_assignments`, `case_groups`, `cases` (+ `ux_cases_unresolved`), `alerts`, `blobs`, `tenant_keys`, `leases` |
| 0002 | events | WP-004 | v0.0 | `events` + append-only trigger |
| 0003 | gate | WP-005 | v0.0 | `proposals`, `approvals`, `policy_grants`, `comms_plans`, `outbox`, `comms` + all INV-01 triggers |
| 0004 | sync | WP-008 | v0.0 | `sync_cursors`, `sync_seen`, `automation_actors` |
| 0005 | first_admin | WP-012 | v0.0 | `ux_role_assignments_bootstrap_admin` (shipped as 0005; shadow became 0006) |
| 0006 | shadow | WP-014 | v0.0 | `shadow_records` |
| 0007 | comms_drafts | WP-102 | v0.1 | `comms_drafts` (owner drafts while `human_owned`) |
| 0008 | case_source | WP-111 | v0.1 | `cases.source` (storm grouping rule c) |
| 0009 | webhooks | WP-113 | v0.1 | `webhook_nonces`, `webhook_inbox` (encrypted) |
| 0011 | memory | WP-108 | v0.1 | `nodes`, `edges`, `kb_docs` (planned as 0101; BM25 computed in the app on both engines) |
| 0010 | feedback | WP-109 | v0.1 | `feedback` (planned as 0102; v0.1 migrations continue the sequence) |
| 0103 | rca | WP-110 | v0.1 | `rca_artifacts` |
| 0201 | lineage | WP-205 | v0.2 | `versions` |
| 0202 | two_person | WP-206 | v0.2 | `approvals` multi-approver support + trigger update |

Rules:
- A migration that changes a trigger recreates it with `DROP TRIGGER IF EXISTS` + `CREATE`, in the same revision, for both dialects.
- Every migration has a downgrade. Downgrades below a release tag are refused unless `--allow-downgrade` is passed.
- CI runs `upgrade head → downgrade base → upgrade head` on both engines (`test_migrations_roundtrip`).

---

## 5. Postgres twins of the INV-01 triggers

SQLite `RAISE(ABORT, msg)` becomes `RAISE EXCEPTION USING MESSAGE = msg, ERRCODE = 'P0001'`. The DB adapter maps both dialects to `AuthorizationDenied` by message prefix (01 §6).

```sql
CREATE OR REPLACE FUNCTION fn_outbox_inv01() RETURNS trigger AS $$
BEGIN
  IF NEW.action_kind = ANY (ARRAY[ /* NEVER_EXECUTABLE_KINDS, generated */ 'create_page']) THEN
    RAISE EXCEPTION USING MESSAGE = 'INV-01: no valid authorization', ERRCODE = 'P0001';
  END IF;
  IF NEW.approval_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM approvals a JOIN proposals p ON p.id = a.proposal_id
       WHERE a.id = NEW.approval_id AND a.status = 'approved' AND p.status = 'approved'
         AND a.payload_hash = NEW.payload_hash AND p.payload_hash = NEW.payload_hash
         AND p.action_kind = NEW.action_kind AND p.tenant_id = NEW.tenant_id) THEN
    RAISE EXCEPTION USING MESSAGE = 'INV-01: no valid authorization', ERRCODE = 'P0001';
  END IF;
  IF NEW.policy_grant_id IS NOT NULL AND (
       NEW.action_kind = ANY (ARRAY[ /* CUSTOMER_FACING_KINDS, generated */
         'customer_comment','send_email_customer','create_ticket_customer','watch_list_add_customer'])
       OR NOT EXISTS (
         SELECT 1 FROM policy_grants g
         WHERE g.id = NEW.policy_grant_id AND g.tenant_id = NEW.tenant_id AND g.status = 'approved'
           AND g.valid_to_ts > extract(epoch FROM now())::bigint
           AND g.action_kind = NEW.action_kind AND g.scope = NEW.scope)) THEN
    RAISE EXCEPTION USING MESSAGE = 'INV-01: no valid authorization', ERRCODE = 'P0001';
  END IF;
  IF NEW.comms_plan_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM comms_plans cp JOIN approvals a ON a.id = cp.approval_id
       WHERE cp.id = NEW.comms_plan_id AND cp.status = 'approved' AND a.status = 'approved'
         AND a.payload_hash = cp.plan_hash AND cp.tenant_id = NEW.tenant_id
         AND NEW.action_kind IN ('customer_comment','send_email_customer')
         AND NEW.payload_json::jsonb ->> 'template_id' = cp.template_id) THEN
    RAISE EXCEPTION USING MESSAGE = 'INV-01: no valid authorization', ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_outbox_inv01 BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION fn_outbox_inv01();
```

`trg_outbox_mark_executed` (AFTER INSERT), `trg_outbox_immutable` (BEFORE UPDATE, comparing `OLD.col IS DISTINCT FROM NEW.col` for each protected column) and `trg_no_self_approval` follow the same pattern. The same test IDs must pass against both engines.
