"""Polls the upstream meter API and publishes every reading to the broker."""

import json
import logging
import os
import time

import httpx
from kafka import KafkaProducer

log = logging.getLogger("feed")


def main() -> None:
    producer = KafkaProducer(
        bootstrap_servers=os.environ["BROKER"],
        value_serializer=lambda v: json.dumps(v).encode(),
    )
    client = httpx.Client(timeout=10)
    since = None
    while True:
        try:
            batch = client.get("https://api.meterfeed.io/v2/readings", params={"since": since})
            readings = batch.json()["readings"]
        except httpx.HTTPError:
            # Logged every pass: the feed keeps logging while it publishes nothing.
            log.warning("upstream unavailable, retrying")
            time.sleep(5)
            continue
        for reading in readings:
            producer.send("readings.raw", reading)
            since = reading["ts"]
        log.info("published %d readings", len(readings))
        time.sleep(1)


if __name__ == "__main__":
    main()
