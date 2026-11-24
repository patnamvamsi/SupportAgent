"""Re-publishes the readings the writer missed, from its cursor onwards. Run by hand."""

import json
import os

import httpx
import psycopg
from kafka import KafkaProducer


def main() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        row = conn.execute("SELECT last_offset FROM ingest_cursor WHERE topic = 'readings.raw'")
        start = row.fetchone()
    producer = KafkaProducer(
        bootstrap_servers=os.environ["BROKER"],
        value_serializer=lambda v: json.dumps(v).encode(),
    )
    history = httpx.get("https://api.meterfeed.io/v2/history", params={"from": start})
    for reading in history.json()["readings"]:
        producer.send("readings.raw", reading)
    producer.flush()


if __name__ == "__main__":
    main()
