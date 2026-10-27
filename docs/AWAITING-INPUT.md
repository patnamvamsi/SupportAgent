# Awaiting input

Items that need the maintainer's decision or resources. The work packages carry on around them; each entry says what is blocked, what was done meanwhile and what unblocks it. Remove an entry when it is resolved.

## Open

### LM Studio down: WP-M08 lab recordings (2026-10-27)

- **Blocked:** the 4 new doc scenarios in `packs/python-postgres/evals/docs-*.yaml` have no recorded Qwen responses, so the CI lab replay fails on them ("responses were not recorded") and the WP-M08 PR can't go green.
- **Meanwhile:** the code is complete and every other gate passes on both DB engines; the 15 existing scenarios replay byte-identically (the v3 prompt renders as v2 without docs). Later WPs are stacked on the WP-M08 branch.
- **Tried:** `lms server start` over ssh on 192.168.0.201 times out waiting for the daemon (it seems to need the desktop session).
- **Unblock:** start LM Studio on 192.168.0.201 with `qwen/qwen3.8-27b` loaded and the server on port 1234, then from the LAN:

  ```
  uv run supportagent eval lab --pack python-postgres --config evals/lab/lab.yaml --live --samples 3 --split all
  ```

  Commit the new recordings (and the baseline report if the run passes), then merge the stacked PRs in order.

### BL-051 Confluence knowledge source

- **Needs:** a Confluence site URL, a space key and a read-only API token (or a decision to use an MCP server for Confluence instead).

### Carried over

- Partner email: the last 50 incidents, the holdout set, and the change source.
- ADR-0003 thresholds (informed by the lab calibration).
- The partner access-model note.
- A self-hosted CI runner on the LAN host (nightly live lab).
- ServiceNow dev password rotation.
- Whether staging stays in assisted mode.
- The v0.0.0 tag.
