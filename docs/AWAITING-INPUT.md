# Awaiting input

Items that need the maintainer's decision or resources. The work packages carry on around them; each entry says what is blocked, what was done meanwhile and what unblocks it. Remove an entry when it is resolved.

## Open

### GitHub Actions won't start jobs: billing (2026-10-28)

- **Blocked:** since PR #70, every CI job fails at once with "The job was not started because recent account payments have failed or your spending limit needs to be increased" (Billing & plans in the GitHub settings). PRs can't get green CI, so nothing merges.
- **Done meanwhile:** the work continues on stacked draft PRs, with every gate run locally on both engines:
  - **#70 BL-039**, citation faithfulness: entail v2 scores 1.000 on 27 pairs (v1 scored 0.926). The lab stays at 24/24 and 5/5.
  - **#71 BL-040**, RCA result on the on-call page. Base: #70.
- **Unblock:** fix the billing or spending limit, then re-run CI on #70 and #71 (`gh run rerun <id>`) and merge them in order.

### BL-051 Confluence: the account email for the live run

- **Done:** the `kind: confluence` source, tested against a recorded fake (BL-051 PR). The site (`vrittitradingplatform.atlassian.net`), space key (`vritti`) and API token are in the git-ignored `.env`.
- **Needs:** the Atlassian account email that owns the token. Confluence Cloud API tokens use basic auth (email + token). Then the opt-in live contract (`tests/contract/test_confluence_live.py`) runs against the space.
- **Also:** the token was pasted in chat, so it is in the session transcript. Consider revoking it after the live run and issuing a new one for the deployment's `.env` only.

## Resolved (2026-10-28)

- **PR stack #62–#66:** live Qwen recordings on every branch (19/19, 20/20, 21/21; 0 false causes; lower bounds 0.832 to 0.845), all CI green, merged to main in order.
- **SOP-driven requests:** option (a), recorded in [ADR-0014](adr/0014-sop-driven-requests.md) part 1. The maintainer's auto-mode direction (per-SOP human opt-in, positive checks and confidence, every request documented, traceable and revertible) is ADR-0014 part 2, *proposed*, tracked as BL-054. It needs review before code because it changes the trust core.
- **Decisions taken for review:** all accepted (WP-M08 before WP-M07, Loki and Alloy on the LAN host, the `pg_disk_full` walkthrough gap, ADR-0013's shadow-only degradation and kept keys).

## Still open, carried over

- Partner email: the last 50 incidents, the holdout set, and the change source.
- ADR-0003 thresholds (informed by the lab calibration).
- The partner access-model note.
- A self-hosted CI runner on the LAN host (nightly live lab).
- ServiceNow dev password rotation.
- Whether staging stays in assisted mode.
- The v0.0.0 tag.
