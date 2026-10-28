# Awaiting input

Items that need the maintainer's decision or resources. The work packages carry on around them; each entry says what is blocked, what was done meanwhile and what unblocks it. Remove an entry when it is resolved.

## Open

### Review and merge the PR stack (#62–#66)

- **Status (2026-10-28):** LM Studio is back and every branch has live Qwen recordings (`qwen/qwen3.8-27b`, `--samples 3 --split all`), with all gates passing and no false causes:

  | PR | Branch | Live lab (python-postgres) | Lower bounds |
  |---|---|---|---|
  | #62 | `wp-m08-knowledge` | 19/19 | 0.832 |
  | #63 | `wp-m07-connector-seam` | same as #62 | |
  | #64 | `wp-m09-extensions` | same as #62 | |
  | #65 | `wp-m09-walkthrough-disk-full` | 20/20 | 0.839 |
  | #66 | `bl-050-logs-search` | 21/21 | 0.845 |

  `common` is 5/5 on every branch. Each branch merges its parent (no force-pushes), so they can merge in order with no rebase.
- **Needs:** your review. Merge in order, #62 to #66. They are drafts because you asked to review everything together.

### BL-051 Confluence knowledge source

- **Needs:** a Confluence site URL, a space key and a read-only API token (or a decision to use an MCP server for Confluence instead).

### WP-M08's last part: SOP-driven requests and runbook distillation

- **Deferred:** both are LLM features that the plan says must be eval-gated. The live lab is back, so runbook distillation can start; SOP-driven requests still need the design call below.
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
