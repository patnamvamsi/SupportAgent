# Restore runbook (SQLite + Litestream)

SupportAgent's SQLite file (`/data/sa.db`) is the system of record. Litestream (`docker/litestream.yml`) streams its WAL to object storage. This runbook brings it back. The named, CI-tested point-in-time restore is v0.2 (ARCH §8.1); until then, rehearse this on staging each release.

## Before you start

- Stop the writers: `supportagent run`, or `serve` and `worker`. Two processes writing during a restore is how you lose data.
- Keep the damaged file: `mv /data/sa.db /data/sa.db.broken`.
- Have the replica credentials in the environment: `LITESTREAM_BUCKET`, `LITESTREAM_ENDPOINT`, `LITESTREAM_ACCESS_KEY_ID`, `LITESTREAM_SECRET_ACCESS_KEY`.

## Restore the latest state

```bash
litestream restore -config docker/litestream.yml -o /data/sa.db /data/sa.db
```

## Restore to a point in time

Pick a moment before the corruption, in UTC:

```bash
litestream restore -config docker/litestream.yml -timestamp 2026-10-21T08:55:00Z -o /data/sa.db /data/sa.db
```

## Verify, then restart

```bash
supportagent migrate
supportagent doctor
```

`doctor`'s `db` check must pass (schema at head, foreign keys on). Then restart the processes.

## What a restore means for in-flight work

- **Outbox rows** that were sent after the restore point but aren't recorded in the restored file are re-sent at most once more. The executor reconciles every journal action against its `[sa:<key>]` marker first (INV-04), so a work note or comment that already landed isn't posted again.
- **ServiceNow sync** resumes from the restored cursor. The overlap window and `(sys_id, sys_mod_count)` dedup re-read anything newer (ARCH §8.1), as long as `sys_audit` retention on the instance covers the gap (`doctor` reports it).
- **Approvals and grants** made after the restore point are lost and must be redone. They are human decisions; nothing replays them.
