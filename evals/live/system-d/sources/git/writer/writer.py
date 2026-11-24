"""Consumes readings into the store, keeps its cursor there, and rolls them up hourly."""

import json
import logging
import os

import psycopg
from apscheduler.schedulers.background import BackgroundScheduler
from kafka import KafkaConsumer, KafkaProducer

log = logging.getLogger("writer")


def rollup_hourly() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            "INSERT INTO readings_hourly (meter_id, hour, total) "
            "SELECT meter_id, date_trunc('hour', ts), sum(value) FROM readings "
            "WHERE ts >= now() - interval '2 hours' GROUP BY 1, 2 ON CONFLICT DO NOTHING"
        )


def main() -> None:
    consumer = KafkaConsumer(
        "readings.raw",
        bootstrap_servers=os.environ["BROKER"],
        group_id="writer",
        enable_auto_commit=False,
    )
    dead_letters = KafkaProducer(bootstrap_servers=os.environ["BROKER"])
    scheduler = BackgroundScheduler()
    scheduler.add_job(rollup_hourly, "cron", minute=5)
    scheduler.start()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for message in consumer:
            try:
                reading = json.loads(message.value)
                conn.execute(
                    "INSERT INTO readings (meter_id, ts, value) VALUES (%s, %s, %s)",
                    (reading["meter_id"], reading["ts"], reading["value"]),
                )
            except (ValueError, KeyError):
                dead_letters.send("readings.dlq", message.value)
            # The cursor: where the writer is, so a backfill knows what it missed.
            conn.execute(
                "UPDATE ingest_cursor SET last_offset = %s WHERE topic = %s",
                (message.offset, message.topic),
            )
            conn.commit()
            consumer.commit()
            log.info("stored offset %d", message.offset)


if __name__ == "__main__":
    main()
