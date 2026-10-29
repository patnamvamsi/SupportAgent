---
title: "Settlement batch runbook"
page_id: "20003"
space: PAY
version: "11"
last_updated: 2026-06-01
labels: [runbook, batch]
---
# Settlement batch

settlement-batch runs as a Kubernetes CronJob at 01:30 UTC. It reads the day's captured
payments from PAYMENTS, writes the settlement records to SETTLEMENT, and uploads
`SETTLE_<date>.csv` to the bank's SFTP server.

Failures we see:

- `ORA-01653: unable to extend table SETTLEMENT.SETTLEMENT_LINES` -> the tablespace is
  full; ask the DBA to add a datafile.
- SFTP timeouts -> the bank's endpoint; retry after 02:30.
