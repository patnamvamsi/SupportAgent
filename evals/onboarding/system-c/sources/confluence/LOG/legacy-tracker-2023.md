---
title: "Tracker architecture (2023)"
page_id: "50002"
space: LOG
version: "21"
last_updated: 2023-05-10
labels: [architecture, legacy]
---
# Tracker architecture (2023)

The **tracker-monolith** (PHP) handled bookings, tracking and notifications in one
application, on the `trackdb` MySQL database. Carrier updates arrived by nightly SFTP.

> Superseded in 2024 by the Shipment Tracking services; the monolith is retired.
