---
title: "Runbook: tracking updates delayed"
page_id: "50003"
space: LOG
version: "6"
last_updated: 2026-07-19
labels: [runbook, rabbitmq]
---
# Runbook: tracking updates delayed

When customers see stale tracking, check the `shipment-events` queue on `logistics-mq` in
the RabbitMQ management UI. If it is growing and **tracking-worker** shows no consumers,
the worker has crashed: check its pods, then scale it back up. The worker writes to
`trackingdb`; its dashboards are in Grafana.
