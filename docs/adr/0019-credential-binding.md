# 0019. Credential binding: a credential is stored only once it is proven read-only

- **Status:** accepted (2026-11-02; roadmap item 1.2 in `docs/next-steps.md`)
- **Date:** 2026-11-02
- **Code PR:** WP-O5b
- **Builds on:** ADR-0015 §4 (access requests and their contracts), ADR-0016 (plugins are data), ADR-0017 (the autopilot)

## Context

Onboarding asks for least-privilege access: one request per plugin contract (`requires`) and target. Each contract already says what proves it:
- `verify_can`: what the credential must be able to read;
- `verify_cannot`: every write it must be refused.

Approving a request recorded intent only. Nothing took the credential, nothing ran the probes, and nothing used the credential afterwards. A real system's access ended at "approved".

## Decision

### 1. Binding is a separate step from approving

A person approves a request in `/ui/onboarding/requests`, then binds its credential from a terminal: `supportagent onboard bind <id>`. The credential is read from a hidden prompt or from `--stdin`. It is never typed into a web page, echoed back, or logged.

Binding:
1. looks up the plugin's contract as it is installed now, not as the plan quoted it;
2. runs every probe with the candidate credential, through the contract's verifier;
3. when every `verify_can` was allowed and every `verify_cannot` refused: stores the credential in the vault and marks the request `bound`;
4. otherwise: stores nothing and reopens the request with the reason (`it can insert, and a read-only account can't: it has INSERT ANY TABLE`).

A bound request can't be declined or rebound until it is unbound (`onboard unbind <id>`, which takes the credential out of the vault and leaves the request approved).

### 2. Probes never write

Each probe either reads, or asks the system what the credential may do:

| Verifier | `verify_can` | `verify_cannot` |
|---|---|---|
| `oracle` | the `SELECT` itself, in a read-only transaction | `SESSION_PRIVS` (for example `INSERT ANY TABLE`, `CREATE TABLE`), `ALL_TAB_PRIVS` granted to the account or its roles, and objects it owns |
| `mongodb` | the command (`currentOp`, a `find` on `system.profile`) | `connectionStatus` with `showPrivileges`: the actions its roles grant. A server without authentication allows every write. |
| `rabbitmq` | `GET /api/...` on the management API | in each vhost: a publish routed to no queue, and a purge and delete of a queue that doesn't exist. *Not found* means the vhost let the user in, which the contract doesn't grant, so it counts as allowed. An `administrator` or `policymaker` tag allows every write. |

Oracle grants to `PUBLIC` are left out of the check. Every account has them, including Oracle's own `PLAN_TABLE`, so they're the DBA's to audit, not something this credential adds.

### 3. Fail closed

A probe the system couldn't answer is an *error*. A probe the verifier doesn't know is an error too. For a `verify_cannot` probe, an error is never taken for a refusal: the credential isn't bound.

A contract that names no verifier can't be proven read-only, so it isn't bound. Its request says so. Verifiers are core code: a plugin names one (`verifier:`) and never ships one. This is because proving a credential read-only is the one check a third-party plugin must not be able to soften.

This core ships verifiers for `oracle`, `mongodb` and `rabbitmq`. The other contracts (GitHub, Snowflake, Kafka, Airflow, Grafana and the rest) stay unbindable until each gets one.

### 4. The vault, never the database

Bound credentials live in one encrypted file, `onboarding.credentials_vault` (default `local/credentials.vault`):
- **Encryption:** AES-256-GCM, with a key derived from the operator's master key (HKDF-SHA256, its own `info`). The tenant id is the additional data.
- **Writes:** the whole file is written to a temporary file that is `0600` before any byte lands, then renamed over the old one.

The database keeps the request's status and the probes' results (`verification_json`, migration 0021). Every binding is also an `access_verified` event in the audit trail, with each probe and its outcome. Every detail is redacted of the credential and any password inside it.

### 5. What a bound credential changes

The tool registry adds the vault's credentials to their backend:
- **First-party backends:** a bound `oracle_ro` credential registers the Oracle tools for its target. A target `tools.oracle` already configures keeps its configured DSN, because the reviewed config wins.
- **MCP backends** (MongoDB, RabbitMQ): the credential is held for the server that serves them. Launching that server with it belongs to the installer (next-steps §3).

### 6. Keeping it true

Privileges change after binding: a DBA grants a role, an admin adds a tag. `supportagent onboard verify` runs every bound credential's probes again. If one can now write, its credential is taken out of the vault and its request reopens with why. It exits non-zero when any credential was unbound, so a scheduler can alert on it. The worker runs it as `onboard.verify` every `onboarding.verify_interval_h` (default 24 hours).

## Consequences

- An approved request is still a need in `onboard status` until it is bound ("approved, bind it with …"). A refused one shows why its credential was refused.
- Least privilege is checked against what the system itself reports, not against what the person who issued the credential believed they granted.
- The verifiers are tested against fakes that answer the way the real systems do (the privilege views, `connectionStatus`, the management API's status codes), not against live systems. The first real system (roadmap 1.4) is where they meet one.
- Done when a fake Oracle, MongoDB and RabbitMQ, each with a read-only and a read-write account, bind the first and refuse the second (`tests/unit/test_access_binding.py`).
