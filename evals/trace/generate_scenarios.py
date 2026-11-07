"""Generate the trace lab's System C scenarios (ADR-0021 §6).

Deterministic: run it again after changing the cases below and commit the
YAML. Each scenario is one shipment's logs across the four services, the
time it is asked about, services whose log store is down, the personal data
seeded into its lines, and the truth: the verdict and the stage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
APPS = {
    "created": "shipments-api",
    "booked": "carrier-adapter",
    "tracking": "tracking-worker",
    "notified": "notifications-service",
}
PEOPLE = [
    ("Jane Doe", "14 Elm Street"),
    ("Arjun Mehta", "7 Harbour Road"),
    ("Maria Garcia", "221 Baker Avenue"),
    ("Chen Wei", "9 Station Lane"),
    ("Olivia Brown", "3 Mill Close"),
    ("Samuel Okafor", "88 King's Parade"),
]
T0 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def _ts(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lines(
    sid: str, person: tuple[str, str], at: dict[str, float], rejected: bool
) -> dict[str, list[list[str]]]:
    name, address = person
    text = {
        "created": f"shipment {sid} created for {name}, deliver to {address}",
        "booked": (
            f"carrier booking REJECTED for {sid}: address not serviceable ({address})"
            if rejected
            else f"carrier booking confirmed for {sid} (FastShip)"
        ),
        "tracking": f"tracking started for {sid}",
        "notified": f"sms sent to {name} for {sid}",
    }
    return {APPS[stage]: [[_ts(m), text[stage]]] for stage, m in at.items()}


CASES: list[dict[str, object]] = [
    # (id, stage times in minutes, asked at, down, rejected, truth kind, truth stage, late)
    *[
        {
            "id": f"complete-{i}",
            "at": {"created": 0, "booked": 5, "tracking": 12, "notified": 15},
            "now": 120,
            "truth": ("complete", None, []),
        }
        for i in range(1, 4)
    ],
    {
        "id": "complete-late-booking",
        "at": {"created": 0, "booked": 40, "tracking": 45, "notified": 50},
        "now": 120,
        "truth": ("complete", None, ["booked"]),
    },
    {
        "id": "complete-late-tracking",
        "at": {"created": 0, "booked": 5, "tracking": 90, "notified": 95},
        "now": 200,
        "truth": ("complete", None, ["tracking"]),
    },
    *[
        {
            "id": f"stuck-booking-{i}",
            "at": {"created": 0},
            "now": 60 + 10 * i,
            "truth": ("stuck", "booked", []),
        }
        for i in range(1, 4)
    ],
    *[
        {
            "id": f"stuck-tracking-{i}",
            "at": {"created": 0, "booked": 5},
            "now": 90 + 10 * i,
            "truth": ("stuck", "tracking", []),
        }
        for i in range(1, 4)
    ],
    *[
        {
            "id": f"stuck-notify-{i}",
            "at": {"created": 0, "booked": 5, "tracking": 10},
            "now": 60,
            "truth": ("stuck", "notified", []),
        }
        for i in range(1, 3)
    ],
    {
        "id": "waiting-booking",
        "at": {"created": 0},
        "now": 10,
        "truth": ("in_progress", "booked", []),
    },
    {
        "id": "waiting-tracking",
        "at": {"created": 0, "booked": 5},
        "now": 20,
        "truth": ("in_progress", "tracking", []),
    },
    *[
        {
            "id": f"rejected-{i}",
            "at": {"created": 0, "booked": 3},
            "rejected": True,
            "now": 60,
            "truth": ("rejected", "booked", []),
        }
        for i in range(1, 4)
    ],
    {"id": "not-found-1", "at": {}, "now": 60, "truth": ("not_found", "created", [])},
    {"id": "not-found-typo", "at": {}, "now": 60, "truth": ("not_found", "created", [])},
    {
        "id": "logs-down-booking",
        "at": {"created": 0},
        "down": ["carrier-adapter"],
        "now": 60,
        "truth": ("cannot_tell", "booked", []),
    },
    {
        "id": "logs-down-tracking",
        "at": {"created": 0, "booked": 5},
        "down": ["tracking-worker"],
        "now": 90,
        "truth": ("cannot_tell", "tracking", []),
    },
    {
        "id": "logs-down-first",
        "at": {},
        "down": ["shipments-api"],
        "now": 60,
        "truth": ("cannot_tell", "created", []),
    },
    # A passed stage the logs don't show (correlation gap): still where it is now.
    {
        "id": "gap-tracking-passed",
        "at": {"created": 0, "booked": 5, "notified": 20},
        "now": 60,
        "truth": ("complete", None, []),
    },
    # A stage down but passed: what comes after decides.
    {
        "id": "down-but-passed",
        "at": {"created": 0, "booked": 5, "tracking": 10},
        "down": ["carrier-adapter"],
        "now": 60,
        "truth": ("stuck", "notified", []),
    },
]


def main() -> None:
    scenarios = []
    for n, case in enumerate(CASES):
        sid = f"SHP-{100001 + n:06d}"
        person = PEOPLE[n % len(PEOPLE)]
        kind, stage, late = case["truth"]  # type: ignore[misc]
        scenarios.append(
            {
                "id": case["id"],
                "object_id": sid,
                "now": _ts(case["now"]),  # type: ignore[arg-type]
                "logs": _lines(sid, person, case["at"], bool(case.get("rejected"))),  # type: ignore[arg-type]
                "down": list(case.get("down", [])),  # type: ignore[call-overload]
                "pii": [
                    {"value": person[0], "entity": "PERSON"},
                    {"value": person[1], "entity": "LOCATION"},
                ],
                "truth": {"kind": kind, "stage": stage, "late": late},
            }
        )
    out = ROOT / "system-c" / "scenarios.yaml"
    header = "# Generated by evals/trace/generate_scenarios.py; edit the script, not this file.\n"
    out.write_text(
        header
        + yaml.safe_dump({"flow": "shipment-tracking", "scenarios": scenarios}, sort_keys=False)
    )


if __name__ == "__main__":
    main()
