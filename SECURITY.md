# Security policy

SupportAgent reads production tickets, logs and records, and holds credentials for them, so security reports are welcome and handled first.

## Reporting a vulnerability

Report it privately through GitHub: **Security → Report a vulnerability** on this repository ([private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)). Please don't open a public issue, pull request or discussion for it.

Include what you found, how to reproduce it, the version (`supportagent --version`) and what an attacker could do with it. You'll get an acknowledgement within 7 days and a plan within 30. The fix ships as a patch release with a GitHub security advisory, crediting you unless you'd rather not be named.

## Supported versions

Before 1.0, only the latest `0.MINOR` release line gets security fixes.

## What counts

The product's promises are the security boundary (see [the architecture](docs/spec/ARCHITECTURE.md)). A way around any of them is a vulnerability:

- **The approval gate:** nothing that changes state or reaches a customer runs without an approval or an admin's grant, enforced in the database (INV-01). No one approves their own proposal.
- **Redaction:** secrets and personal data are redacted before any text reaches a language model (INV-06), and tracing pseudonymises PII before the model sees it (ADR-0021).
- **Read-only tools:** checks and record reads can't write. The SQL, LogQL and other queries are reviewed text, never written by the model.
- **Credentials:** the ones SupportAgent stores are sealed at rest under the operator's master key, and no credential is ever logged, sent to a model or shown.
- **Prompt injection:** ticket, log or document text that makes the agent propose or run something it shouldn't.

Out of scope: problems that need an admin's own credentials or a modified deployment, missing hardening on a deployment that ignores the docs (for example, exposing the UI without TLS), and denial of service against a single-tenant install by its own users.
