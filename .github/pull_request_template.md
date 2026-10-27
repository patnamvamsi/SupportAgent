<!-- 05 §7: WP id, spec sections implemented, test IDs added, DoD checklist,
     screenshots for UI, and the ADR link if the spec changed. -->

## WP

`WP-0NN` — <title> (10-wp-*)

## Spec sections implemented

- 0x §N — <what>
- ARCH §N — <what>

## Test IDs added

Tests named in the WP exist with the exact IDs and were committed red first:

- `test_...`
- `test_...`

## DoD checklist

- [ ] <DoD item from the WP, verbatim>
- [ ] CI green (lint, typecheck, architecture, unit+property, contract,
      invariants on SQLite **and** Postgres, integration, e2e, security)

## Lab report (pack, prompt, tool or model changes)

Run `supportagent eval lab --pack <id> --live --samples 3 --split all` and paste:

- before → after (passed/total, false causes, classification LB):
- per-case diff against the baseline (newly passing / newly failing):
- `supportagent pack validate --strict packs/<id>`: ok

N/A (no pack, prompt, tool or model change)

## Screenshots

N/A (no UI in this PR)

## ADR link

N/A (spec unchanged)
