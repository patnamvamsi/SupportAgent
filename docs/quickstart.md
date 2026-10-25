# Quickstart

From a clean checkout to a running shadow deployment against your ServiceNow instance. Shadow mode **never writes to ServiceNow**: the agent reads tickets, triages them, and records what it *would* have done.

## 1. Install

```bash
uv sync
supportagent --version
```

## 2. Configure

Copy the examples and edit them for your tenant: instance URL, integration user, catalog services, routing.

```bash
cp supportagent.example.yaml supportagent.yaml
cp catalog/services.example.yaml catalog/services.yaml
```

Secrets are never in the file. Each `*_env` key names an environment variable:

```bash
export SUPPORTAGENT_DB_URL=sqlite:///data/sa.db
export SA_MASTER_KEY_ENV=$(python -c "import secrets; print(secrets.token_hex(32))")
export SN_CLIENT_ID=... SN_CLIENT_SECRET=...
export OIDC_CLIENT_ID=... OIDC_CLIENT_SECRET=...
export SA_SESSION_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export ANTHROPIC_API_KEY=...
export PAGERDUTY_TOKEN=...      # paging: attach-only (enrich_page, page lookups); or comment out paging:
```

### The ServiceNow integration user

Create a dedicated user (for example `svc_supportagent`) with the `itil` role, and nothing broader: `doctor` warns about `admin`, `security_admin` or any role it can't read. Sync also reads each changed record's history, so the user needs **read access to `sys_audit` and `sys_journal_field`**. ServiceNow restricts both tables by default. Create a role (for example `x_supportagent_history`), add a read ACL for it on each table (`sys_audit` and `sys_journal_field`, both the table and its fields), and grant it to the user. `doctor` fails `servicenow.history_access` until this is done.

## 3. Database and checks

```bash
supportagent migrate
supportagent doctor --setup --discover-automation
```

`doctor` checks the config, the database, ServiceNow read access and journal visibility, paging integrations, the LLM and your identity provider, and never writes to ServiceNow. `--setup` requests the default T1 grants; approve them at `/ui/setup`. `--discover-automation` lists the non-interactive users that assign tickets; confirm or reject each one there too.

## 4. Calibrate

```bash
supportagent eval run --suite smoke
supportagent eval run --suite smoke --live --write-calibration --config supportagent.yaml
```

The first run is offline (a scripted LLM) and checks the wiring. The second calls your model and writes `evals/artifacts/calibration.json`.

## 5. Run

```bash
supportagent run --host 0.0.0.0 --port 8000
```

`run` starts the admin UI and API **and** the worker, which polls ServiceNow and drains webhooks. In a split deployment, run `supportagent serve` and `supportagent worker` separately. Sign in at `http://localhost:8000/ui/login`: the first person to sign in becomes admin.

## 6. Watch the shadow run

```bash
supportagent stats --mode shadow --window 30d
supportagent shadow import exports/last-50-incidents.jsonl
```

`/ui/stats` shows triage agreement with its Wilson lower bound, the promotion gate (CAP §4.7) and approval fatigue. `shadow import` replays a history export, which is how the provisional thresholds get set (ADR-0003). Promotion to assisted mode is your decision: set `mode: assisted` once the gate passes and every automation-actor candidate is decided in `/ui/setup`. The worker then sends approved and grant-covered actions (ADR-0008).
