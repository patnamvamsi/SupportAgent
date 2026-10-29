# Awaiting input

Items that need the maintainer's decision or resources. The work packages carry on around them; each entry says what is blocked, what was done meanwhile and what unblocks it. Remove an entry when it is resolved.

## Open

### ADR-0015 system onboarding and plugins: review before code (2026-10-29)

- **What:** [ADR-0015](adr/0015-system-onboarding-and-plugins.md) is proposed. An instance pointed at a system reads its docs, ServiceNow history and manifests, and builds a system graph. It then works out which plugins and read-only access it needs and asks for them, sets itself up once they're granted, and proves readiness per incident class. Skills, MCP servers, connectors and knowledge sources become one plugin model.
- **Needs your answers on:** one instance per system; where plugins live and whether third-party plugins are accepted; the token and time budget for reading the docs; SME review time; live discovery with read-only prod access; the readiness bar (proposed: 70% of incident weight).
- **Unblocks:** WP-O7 (the onboarding lab with reference systems A and B) and WP-O1 (the plugin model), which come first.

### GitHub Actions minutes: merging on local CI until they reset (2026-10-29)

- **What happened:** the repo is private on the free plan, so Actions get the free plan's monthly minutes. Each push runs about 13 jobs, billed per job and rounded up to the minute, and the nightly job runs every day; together they used up the month's minutes. GitHub then refuses jobs with the "payments … spending limit" message. No payment is needed.
- **Done meanwhile:** `scripts/ci-local.sh` (`make ci-local`, #72) runs every `ci.yml` job on this machine with the same commands. #70, #71 and BL-054 were merged after a full local run, with its table posted on each PR.
- **Optional:** nothing is blocked. When the minutes reset, GitHub CI runs again on the next push. To use fewer minutes from then on, either:
  - run the nightly job less often; or
  - set up the self-hosted runner on the LAN host (carried over below). Its minutes are free on private repos.

### Confluence: revoke the pasted token; label pages if you want them scoped

- **Done:** the live contract passed against `vritti` on 2026-10-29: 3 pages read, GET only, every chunk cited with its URL and version.
- **Your call:**
  - The API token was pasted in chat, so it is in the session transcript. Consider revoking it and keeping a fresh one only in the deployment's `.env`.
  - None of the 3 pages has a `hypothesis-<id>` or `service-<name>` label yet. They are read as internal guidance for every service and suggest no checks.

## Resolved (2026-10-29)

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
- The v0.0.0 tag.
