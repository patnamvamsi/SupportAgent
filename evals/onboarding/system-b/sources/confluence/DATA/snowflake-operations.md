---
title: "Snowflake operations"
page_id: "40003"
space: DATA
version: "12"
last_updated: 2026-08-30
labels: [runbook, snowflake]
---
# Snowflake operations

- **Warehouse queuing**: `WH_BI` queues at month start; raise the max cluster count.
- **Task failures**: `TASK_BUILD_DAILY_SALES` fails when a RAW column changes; read
  `TASK_HISTORY` and fix the SQL.
- **Credit quota**: `RM_ANALYTICS` suspends warehouses at 100%; finance approves increases.

Read-only diagnosis needs a role with `ACCOUNT_USAGE` (query, task and warehouse history)
and `USAGE` plus `MONITOR` on the warehouses. The `SUPPORT_RO` role has exactly that.
