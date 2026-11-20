# Awaiting input

Items that need the maintainer's decision or resources. The work packages carry on around them; each entry says what is blocked, what was done meanwhile and what unblocks it. Remove an entry when it is resolved.

## Open

### Acting on a system, and the name (2026-11-20)

- **Acting (ADR-0022 §6, WP-218):** may SupportAgent execute approved recovery steps on the proving ground (container restarts, a reviewed single-row update such as advancing a cursor), under per-step approval or a conditioned SOP grant, in paper mode? Until you say so, v0.2 proposes runbooks with dry runs and a person carries them out.
- **The name:** keep SupportAgent, or rename before outside users depend on it.

### Confluence token and ServiceNow dev password (carried from going public)

- The repo is public. Older commits name the LAN host, the ServiceNow developer instance and the Confluence site: no credentials (a full-history gitleaks scan is clean), but revoke the Confluence token pasted in an earlier session and rotate the ServiceNow dev password if you haven't.

### A real system to onboard (2026-11-01)

- **What's needed:** a system to point `supportagent onboard run` at: its ServiceNow scope (business service, assignment groups), Confluence spaces and repositories, plus read-only credentials as onboarding asks for them. The partner data (BL-037) would do.
- **Done meanwhile:** the loop works end to end on three synthetic reference systems (ADR-0015/16/17). [next-steps.md](next-steps.md) lists what must land first for a real system to reach *ready*: live evidence capture (1.1) and credential binding (1.2).
- **Unblocks:** next-steps 1.4, the first real onboarding.

### Confluence: revoke the pasted token; label pages if you want them scoped

- **Done:** the live contract passed against your Confluence space on 2026-10-29: 3 pages read, GET only, every chunk cited with its URL and version.
- **Your call:**
  - The API token was pasted in chat, so it is in the session transcript. Consider revoking it and keeping a fresh one only in the deployment's `.env`.
  - None of the 3 pages has a `hypothesis-<id>` or `service-<name>` label yet. They are read as internal guidance for every service and suggest no checks.

## Resolved (2026-11-20)

- **Going public:** the repo is public. Private vulnerability reporting, secret scanning with push protection, Dependabot alerts and security updates are on. The `main` ruleset requires a pull request and the CI checks, with no force pushes or deletion; `v*` tags can't be moved or deleted. The `release` environment deploys from `v*` tags only and needs the maintainer's approval.
- **Actions minutes:** public repositories run standard GitHub-hosted runners free, so CI and the nightly soak run on GitHub again; `make ci-local` stays for local use.
- **First release:** 0.1.0, tagged now, ahead of the partner-dependent v0.1 exit criteria (the holdout gates, BL-037, and the exit demo), which stay open. 0.0.0 was never tagged.

## Resolved (2026-11-08)

- **ADR-0021 (transaction tracing):** accepted. A flow is usable once its own lab passes, without the calibration floor. The first flow is picked on the real system from candidates onboarding proposes. Tracing uses logs and messages first and reads business records only where they can't establish a stage; PII is pseudonymised with Presidio before the model sees anything.

## Resolved (2026-10-29)

- **ADR-0015 (system onboarding, plugins):** accepted. One instance per system; plugins as proposed (third parties ship data plugins and MCP servers; in-process code is ours, signed); onboarding uses the frontier model when both a local and a frontier model are configured; SME review made easy visually; read-only prod access for discovery and triage; readiness bar 70%.
- **ADR-0014 part 2 (SOP auto mode):** reviewed ("go ahead") and delivered as BL-054. Auto mode is a grant an admin approves on one SOP step, with conditions that must hold every time it is used: named checks positive, the cause's claim passing `entail`, a daily cap, optional services. If any condition fails or can't be read, the step goes to a person. Every decision is recorded on the case, and each auto note can be retracted from the case page. The two open questions went as proposed: only `work_note` and `enrich_page` declare a revert, and there is no confidence condition until the ADR-0003 thresholds are calibrated.
- **Confluence account email:** received; the live contract passes.
- **#71 fix found while merging:** RCA's gate had no outbox writer, so a page note covered by the `enrich_page` grant was approved but never sent. Fixed and tested in #71 before it merged.

## Resolved (2026-10-28)

- **PR stack #62–#66:** live Qwen recordings on every branch (19/19, 20/20, 21/21; 0 false causes; lower bounds 0.832 to 0.845), all CI green, merged to main in order.
- **SOP-driven requests:** option (a), recorded in [ADR-0014](adr/0014-sop-driven-requests.md) part 1.
- **Decisions taken for review:** all accepted (WP-M08 before WP-M07, Loki and Alloy on the LAN host, the `pg_disk_full` walkthrough gap, ADR-0013's shadow-only degradation and kept keys).

## Still open, carried over

- Partner email: the last 50 incidents, the holdout set, and the change source.
- ADR-0003 thresholds (informed by the lab calibration).
- The partner access-model note.
- A self-hosted CI runner on the LAN host (nightly live lab).
- ServiceNow dev password rotation.
- Whether staging stays in assisted mode.
