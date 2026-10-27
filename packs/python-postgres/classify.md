How symptoms map to labels for a Python service on Postgres:
- Pool or connection errors (QueuePool limit, too many clients, connection slots reserved): database/saturation.
- Queries or pages slow, writes hanging on the database, or data that stays stale after a write (an order status that doesn't update): database/latency. Stale reads usually mean a lagging read replica, not a batch job.
- The database refuses connections or is down: database/failure.
- Gunicorn or uvicorn workers timing out (WORKER TIMEOUT, 502/504 from the proxy) while waiting on something other than the database: app/worker_timeout.
- OOMKilled pods, exit code 137, MemoryError: app/memory.
- Startup failures or misbehaviour right after a release with configuration errors (ImproperlyConfigured, a missing environment variable): app/config.
- Celery or RQ tasks delayed while the queue grows: queue/backlog.
- TLS errors (certificate verify failed, certificate has expired): edge/certificate.
- 429s or load shedding during a surge of requests: request/capacity.
