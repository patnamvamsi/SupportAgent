# Data handling

SupportAgent is self-hosted. It runs where you deploy it, reads your systems with the read-only access you grant, and sends nothing to its authors: there is no telemetry and no phone-home. This page says what it reads, what it keeps and what leaves your deployment.

## What leaves your deployment

**Only requests to the language model you configure.** The model sees ticket text, check results, log lines and document excerpts, after redaction:

- **Secrets and personal data are redacted before any model call.** The redactor sits inside the one client every model call goes through (INV-06), so no feature can skip it.
- **Tracing goes further (ADR-0021).** Every stage result goes through [Presidio](https://microsoft.github.io/presidio/), which runs locally, and personal data becomes a stable placeholder (`<PERSON_1>`). The map back stays in memory and is never stored. If the analyser isn't installed, no stage result reaches the model.
- **Business records:** reads are reviewed queries over a read-only role, and only the fields a flow lists reach the model.

**To keep everything in-house, use a local model:** any OpenAI-compatible server (LM Studio, vLLM, llama.cpp) or Ollama. Then nothing leaves your network. With a hosted provider (Anthropic, OpenAI), its own data terms apply to the redacted requests.

Writes to your systems (ticket notes, page notes) happen only after a person's approval or an admin's grant, and go only to the systems you connect.

## What it keeps

All of it is in your database (Postgres, or SQLite for a single node).

| Data | Kept for | Notes |
|---|---|---|
| Incoming alerts and the raw ticket payloads | `retention.alerts_days` (default 90) | |
| Cases, their classification and proposals | Not swept | Case memory reads resolved cases. Delete a tenant's data directly if you need to. |
| The event log (every decision, tool call and approval) | `retention.events_days` (default 395) | Tool arguments are redacted. Model prompts aren't stored, only their hashes and token counts. |
| Communications it drafted or sent | `retention.comms_days` (default 395) | |
| Captured evidence | `onboarding.retention_days`, per capability | ADR-0018 |

An hourly `retention.sweep` deletes what's past its period.

## Credentials

- They come from environment variables, which the config only names, or from the built-in vault.
- Vault entries are sealed with AES-256-GCM under a key derived from your master key (`SA_MASTER_KEY_ENV`).
- Credentials are never logged, sent to a model or shown in the UI.
- Grant read-only access wherever you can: onboarding asks for the narrowest access each check needs, and binding a credential (ADR-0019) proves it can't write.

## Your responsibilities as the operator

- Serve the UI over TLS and behind your identity provider.
- Back up the database. See [restore](ops/restore.md).
- Choose the model provider, and check that its terms fit your data.
- Keep the master key out of the database's backups.
