---
title: "Data platform on-call runbook"
page_id: "40001"
space: DATA
version: "27"
last_updated: 2026-10-02
labels: [runbook, oncall]
---
# On-call runbook

Most pages are about **freshness**: a table or a dashboard is behind.

1. Find the stale table, then walk upstream: Streamlit page -> ANALYTICS table ->
   the task or Spark job that builds it -> its RAW tables -> the connector or stream
   that loads them -> the Kafka topic.
2. For streaming: check consumer group lag (`stream-ingest`) and the Spark streaming
   query progress on EMR.
3. For the connector: Kafka Connect task status of `orders-cdc-ingest`.
4. For Snowflake: `TASK_HISTORY`, `QUERY_HISTORY` (queued time) and the resource monitor.
5. For batch: the Airflow task log and the Spark history server for the failed stage.
