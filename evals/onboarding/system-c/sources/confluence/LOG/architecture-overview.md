---
title: "Shipment Tracking architecture overview"
page_id: "50001"
space: LOG
version: "9"
last_updated: 2026-09-02
labels: [architecture, logistics]
---
# Shipment Tracking architecture overview

Shipment Tracking takes shipments from the web shop, books them with carriers and keeps
customers up to date. It is owned by **team-logistics** (on call: LOG-L2). Everything runs
on Kubernetes in the `logistics` namespace of the `prod-us2` cluster.

## Services

- **shipments-api** (Node.js, Express) is the shop's entry point. It stores shipments in
  the `shipmentsdb` MongoDB database and publishes every status change to the
  `shipment-events` queue on the `logistics-mq` RabbitMQ broker. It calls
  **carrier-adapter** to book a shipment.
- **carrier-adapter** (Node.js, Express) talks to the FastShip carrier API
  (`api.fastship.example`) and publishes the carrier's scan events to `carrier-scans`.
- **tracking-worker** (Node.js) consumes `shipment-events` and `carrier-scans` and keeps
  each shipment's timeline in the `trackingdb` MongoDB database.
- **notifications-service** (Node.js, Express) consumes `shipment-events` and texts
  customers through the SMSGo API (`api.smsgo.example`). It has no database of its own.

## Observability

Logs and metrics are in Grafana at `grafana.logistics.acme.internal` (Loki and Prometheus).
