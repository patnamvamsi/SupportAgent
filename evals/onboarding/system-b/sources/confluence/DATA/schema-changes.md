---
title: "Schema change process"
page_id: "40005"
space: DATA
version: "5"
last_updated: 2026-04-03
labels: [process, kafka]
---
# Schema changes

Producers must register backward-compatible Avro schemas. An incompatible change to
`events.orders` or `cdc.customers` stops `orders-cdc-ingest` and fills its dead-letter
topic.
