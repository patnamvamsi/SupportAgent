---
title: "Oracle lock contention runbook"
page_id: "20002"
space: PAY
version: "9"
last_updated: 2026-07-14
labels: [runbook, oracle]
---
# Oracle lock contention

Symptoms: payment-service captures time out, `ORA-00060: deadlock detected` or long
`enq: TX - row lock contention` waits in PAYDB.

1. Find blockers: `V$SESSION` where `BLOCKING_SESSION` is set; `V$LOCK`.
2. Look at the SQL of the blocker in `V$SQL`.
3. If it is a stuck capture retry, the DBA kills the session. SupportAgent never does.

We need read access to `V$SESSION`, `V$LOCK`, `V$SQL` and `DBA_HIST_ACTIVE_SESS_HISTORY`
for this; the `support_ro` account has it.
