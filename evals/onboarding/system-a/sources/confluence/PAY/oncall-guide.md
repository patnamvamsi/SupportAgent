---
title: "PAY on-call guide"
page_id: "20001"
space: PAY
version: "22"
last_updated: 2026-09-30
labels: [runbook, oncall]
---
# PAY on-call guide

**Who:** PAY-L2 (business hours) and the PAY on-call rota (PagerDuty service PAYPROD).

## First five minutes

1. Open the *Payments overview* Grafana dashboard: 5xx rate per service, p99 latency,
   Hikari active/pending connections, JVM heap.
2. Check the last deploys: GitHub Actions in `acme/payments-*`.
3. Check CardCo's status page when payment-service authorisations time out.

## Known problems

- **Deadlocks in PAYMENTS** (`ORA-00060`): usually two capture retries updating the same
  rows. See *Oracle lock contention runbook*.
- **orders-service OutOfMemoryError**: the order export endpoint loads whole result
  sets; restart the pod and check heap dumps.
- **Connection pool exhaustion** (`HikariPool-1 - Connection is not available, request
  timed out after 30000ms`): a slow query or a lock holds connections; payments-gateway
  then returns 503.
- **Settlement batch fails with `ORA-01653`**: the SETTLEMENT tablespace is full.
