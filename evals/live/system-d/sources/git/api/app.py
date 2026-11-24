"""Serves readings from the store."""

import os

import psycopg
from fastapi import FastAPI

app = FastAPI()


def _query(sql: str, *args: object) -> list[tuple[object, ...]]:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        return conn.execute(sql, args).fetchall()


@app.get("/meters/{meter_id}/readings")
def readings(meter_id: str) -> list[tuple[object, ...]]:
    return _query(
        "SELECT ts, value FROM readings WHERE meter_id = %s ORDER BY ts DESC LIMIT 500", meter_id
    )


@app.get("/meters/{meter_id}/hourly")
def hourly(meter_id: str) -> list[tuple[object, ...]]:
    return _query("SELECT hour, total FROM readings_hourly WHERE meter_id = %s", meter_id)
