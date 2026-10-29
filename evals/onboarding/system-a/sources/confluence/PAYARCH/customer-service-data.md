---
title: "customer-service data ownership"
page_id: "10004"
space: PAYARCH
version: "5"
last_updated: 2026-03-10
labels: [architecture, data]
---
# customer-service data

customer-service reads and writes the CUSTOMERS schema in CRMDB through the
`crm_app` account. (The CMDB still shows a dependency on PAYDB from before the 2024
split; that relationship is wrong and a correction ticket is open.)
