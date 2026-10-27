# Awaiting input

Items that need the maintainer's decision or resources. The work packages carry on around them; each entry says what is blocked, what was done meanwhile and what unblocks it. Remove an entry when it is resolved.

## Open

### LM Studio down: live lab recordings for the PR stack (2026-10-27)

- **Blocked:** CI replays the incident lab from recorded Qwen responses. New scenarios and changed prompts have no recordings yet, so each PR's `eval` job fails with "responses were not recorded". Every other check passes. All the PRs are drafts.
- **The stack, merge in order:**

  | PR | Branch | Needs recordings? |
  |---|---|---|
  | #62 | `wp-m08-knowledge` | yes: 4 doc scenarios |
  | #63 | `wp-m07-connector-seam` | no new prompts (same recordings as #62) |
  | #64 | `wp-m09-extensions` | no new prompts |
  | #65 | `wp-m09-walkthrough-disk-full` | yes: new pack hypothesis, so every python-postgres prompt changes |
  | #66 | `bl-050-logs-search` | yes: 1 new scenario (on top of #65's) |

- **Tried:** `lms server start` over ssh on 192.168.0.201 times out waiting for the daemon (it seems to need the desktop session).
- **Unblock:** start LM Studio on 192.168.0.201 with `qwen/qwen3.8-27b` loaded and the server on port 1234. Then, from the LAN, on each branch marked "yes":

  ```
  uv run supportagent eval lab --pack python-postgres --config evals/lab/lab.yaml --live --samples 3 --split all
  ```

  1. On #62, record and commit (the baseline too if the gates pass), then merge #62, #63 and #64 in order.
  2. Rebase #65 and #66 onto main.
  3. On #66's branch, run once more (it records everything #65 and #66 need), and commit the recordings to #65 or split them.
  4. Merge #65, then #66.

  The walkthrough doc (`docs/walkthroughs/wp-m09-disk-full.md`) gets the live numbers.

### BL-051 Confluence knowledge source

- **Needs:** a Confluence site URL, a space key and a read-only API token (or a decision to use an MCP server for Confluence instead).

### WP-M08's last part: SOP-driven requests and runbook distillation

- **Deferred:** both are LLM features that the plan says must be eval-gated, so they wait until the lab can run live again.
- **SOP-driven requests** also need a design call: how a written procedure maps onto the typed action kinds and the gate. Options: (a) SOP steps only *propose* existing action kinds, each through the gate as today; (b) a reviewed SOP becomes a runbook-like file that names action kinds. (a) needs no trust-core change.
- **Runbook distillation** (team docs to draft runbooks, reviewed by an analyst, then lab-gated) needs no decision; it waits only for the live lab.

## Decisions taken for review

Calls made without the maintainer so work could continue. Each can be reversed in a small PR.

- **WP-M08 before WP-M07** (approved in chat, 2026-10-27).
- **BL-050 (#66):** Loki and Alloy were added to the observability stack on 192.168.0.201 (ports 3110 for Loki; Alloy tails `~/supportagent-observability/demo-logs/`). To remove them: `docker compose rm -sf loki alloy` there.
- **WP-M09 walkthrough gap:** `pg_disk_full` (writes fail on a full data volume) was picked as the plain-language gap.
- **ADR-0013 (WP-M07):**
  - A connector without change history runs **shadow-only**, which is stricter than ADR-0011 §3's "every write needs a human approval". That approval-only mode is BL-053.
  - Stored keys keep their v0.0 values (`em_alert`, `servicenow:` scopes), so nothing is migrated.

## Still open, carried over

- Partner email: the last 50 incidents, the holdout set, and the change source.
- ADR-0003 thresholds (informed by the lab calibration).
- The partner access-model note.
- A self-hosted CI runner on the LAN host (nightly live lab).
- ServiceNow dev password rotation.
- Whether staging stays in assisted mode.
- The v0.0.0 tag.
