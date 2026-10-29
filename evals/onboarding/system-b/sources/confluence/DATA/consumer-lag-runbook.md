---
title: "Kafka consumer lag runbook"
page_id: "40002"
space: DATA
version: "8"
last_updated: 2026-07-21
labels: [runbook, kafka]
---
# Consumer lag on events.clickstream

Lag grows when the streaming job loses executors or a rebalance storms after a broker
restart. Check: consumer group `stream-ingest` lag per partition, EMR executor count,
the last checkpoint. Scale executors or restart from the last good checkpoint.
