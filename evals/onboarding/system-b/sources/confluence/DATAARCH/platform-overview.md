---
title: "Customer Analytics Data Platform overview"
page_id: "30001"
space: DATAARCH
version: "19"
last_updated: 2026-09-05
labels: [architecture, data-platform]
---
# Customer Analytics Data Platform

Owned by **team-data-platform** (support queue DATA-PLATFORM). It turns web-shop events
into the sales and customer metrics the business reads in the *customer-insights* app.

## Flow

1. The web shop publishes to Kafka (Amazon MSK cluster `analytics-msk`):
   `events.clickstream`, `events.orders` and `cdc.customers`. Schemas are Avro in the
   schema registry.
2. **stream-ingest** is a PySpark Structured Streaming job on the EMR cluster
   `emr-streaming`. It consumes `events.clickstream` (consumer group `stream-ingest`)
   and appends to Snowflake `RAW.CLICKSTREAM`.
3. **orders-cdc-ingest** is a Kafka Connect Snowflake sink connector. It consumes
   `events.orders` and `cdc.customers` and loads `RAW.ORDERS` and `RAW.CUSTOMERS`
   (warehouse `WH_LOAD`).
4. **daily-sales-task** is the Snowflake task `TASK_BUILD_DAILY_SALES` on warehouse
   `WH_TRANSFORM`, triggered by the Airflow DAG `daily_sales` at 03:00 UTC. It reads
   `RAW.ORDERS` and rebuilds `ANALYTICS.DAILY_SALES`.
5. **batch-features** is a nightly PySpark batch job on EMR, run by the Airflow DAG
   `customer_features`. It joins `RAW.CLICKSTREAM` with `RAW.CUSTOMERS` and writes
   `ANALYTICS.CUSTOMER_FEATURES`.
6. **customer-insights** is a Streamlit app on Kubernetes (cluster `prod-us1`, namespace
   `analytics-apps`). It reads `ANALYTICS.DAILY_SALES` and `ANALYTICS.CUSTOMER_FEATURES`
   through warehouse `WH_BI`.

Airflow is Amazon MWAA (`analytics-airflow`). Snowflake credits are capped by the
resource monitor `RM_ANALYTICS`.
