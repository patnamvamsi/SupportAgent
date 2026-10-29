---
title: "Refund service retirement"
page_id: "10003"
space: PAYARCH
version: "2"
last_updated: 2025-02-20
labels: [architecture, decision]
---
# Decision: retire refund-service

Refunds moved into payment-service (`POST /payments/{id}/refunds`) in release 2025.2.
refund-service was switched off on 2025-02-28. Its CMDB record is kept as *Retired*
for audit.
