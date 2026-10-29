---
title: "Spark jobs"
page_id: "40004"
space: DATA
version: "9"
last_updated: 2026-05-12
labels: [runbook, spark]
---
# Spark jobs

batch-features fails on executor OOM when the customer_id join skews. Look at the
failed stage in the Spark history server on `emr-batch`, then salt the join or raise
executor memory. stream-ingest runs on `emr-streaming`.
