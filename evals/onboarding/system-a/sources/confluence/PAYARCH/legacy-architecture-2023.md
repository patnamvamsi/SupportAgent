---
title: "Legacy architecture (2023, superseded)"
page_id: "10002"
space: PAYARCH
version: "3"
last_updated: 2023-04-02
labels: [architecture, legacy]
---
# Legacy architecture (2023)

> This page describes the platform before the 2024 migration.

In 2023 orders-service stored orders in a MySQL database, `ordersdb`, on a VM, and a
separate refund-service handled refunds against PAYDB. Payments were processed by the
monolith `payments-core` running on WebLogic.
