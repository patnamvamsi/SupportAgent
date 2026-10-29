---
title: "Payments portal troubleshooting"
page_id: "20004"
space: PAY
version: "6"
last_updated: 2026-05-18
labels: [runbook, ui]
---
# payments-portal troubleshooting

The portal is an Angular SPA on the CDN. A blank page after a release is almost always
a stale `main.<hash>.js` in the CDN cache or a CORS rule on payments-gateway that no
longer allows the portal's origin. Check the browser console and the gateway's CORS
config (`spring.cloud.gateway.globalcors`).
