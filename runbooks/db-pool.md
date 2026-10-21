# Runbook: database pool saturation

Applies to `database/saturation`. The executable form is `db-pool.yaml`; this page is for people.

1. **A recent change?** Look for a failed CI run in the service's repo. If there is one, stop: that's the likeliest cause.
2. **Capacity?** Grep the service's logs for pool exhaustion or connection timeouts.

The agent reports what the evidence shows (SUPPORTED / CONSISTENT_UNCONFIRMED / INCONCLUSIVE), never a confirmed cause, and never to the customer.
