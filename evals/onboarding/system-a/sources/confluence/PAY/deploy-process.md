---
title: "Deploy process"
page_id: "20005"
space: PAY
version: "8"
last_updated: 2026-09-02
labels: [process, deploy]
---
# Deploys

Every service is deployed by its GitHub Actions workflow (`deploy.yml`) with Helm to
`prod-eu1`, namespace `payments`. A bad deploy is rolled back with `helm rollback`.
Releases are announced in #payments-releases with the change number.
