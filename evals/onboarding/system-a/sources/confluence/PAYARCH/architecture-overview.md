---
title: "Payments Platform architecture overview"
page_id: "10001"
space: PAYARCH
version: "14"
last_updated: 2026-08-12
labels: [architecture, payments]
---
# Payments Platform architecture overview

The Payments Platform takes card payments for the Acme web shop and settles them with
the acquiring bank every night. It is owned by **team-payments** (on call: PAY-L2).

## Components

All services are Spring Boot 3 applications on Java 21, deployed to the `payments`
namespace of the `prod-eu1` Kubernetes cluster.

| Component | What it does | Talks to |
|---|---|---|
| payments-gateway | Spring Cloud Gateway in front of every API; rate limiting, auth | orders-service, payment-service, customer-service |
| orders-service | Creates and tracks orders | Oracle `PAYDB` (schema ORDERS), payment-service |
| payment-service | Authorises and captures card payments | Oracle `PAYDB` (schema PAYMENTS), the external card processor (CardCo) |
| customer-service | Customer profiles and saved cards | Oracle `CRMDB` (schema CUSTOMERS) |
| settlement-batch | Nightly Spring Batch job (01:30 UTC): builds the settlement file and sends it to the bank | reads PAYMENTS, writes SETTLEMENT (both `PAYDB`), bank SFTP |
| payments-portal | Angular 17 back-office UI for the payments operations team | payments-gateway only |

The portal is a static Angular build served from the CDN; every call it makes goes
through payments-gateway.

## Data

Two Oracle 19c databases:

- **PAYDB** holds three schemas: ORDERS, PAYMENTS and SETTLEMENT.
- **CRMDB** holds CUSTOMERS. It is owned by the CRM team; we only read and write our
  own schema.

Each service has its own HikariCP pool (default 20 connections; payment-service 40).

## Observability

Logs go to Loki, metrics to Prometheus, both behind Grafana (`grafana.acme.internal`).
Deploys are GitHub Actions workflows in the `acme/payments-*` repositories that run
`helm upgrade` against `prod-eu1`.
