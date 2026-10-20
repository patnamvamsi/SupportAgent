"""Build the WP-014 DoD fixture: 50 incidents, the LLM script, the golden report.

Run ``uv run python tests/data/shadow/build_fixture.py`` to regenerate. The
golden report is computed here **from each group's intended outcome** (the
table below), independently of ``StatsService``: the DoD test then checks that
the real pipeline + ``StatsService`` reproduce it.

Groups (catalog: CIPAYMENTS, CISEARCH; min_score 0.80; shipped preclass rules):

=====  ====  ===============================================  ==========================
group  n     setup                                            intended decision / agreement
=====  ====  ===============================================  ==========================
A1     15    keyword subject, LLM = preclass label, P3        routed / agree
A2     5     "refused" (database/failure), human sibling      routed / agree (same domain)
B      5     like A1, human severity P2                       routed / disagree
C      5     "deploy" subject, LLM network/outage             classifier-conflict / 2 agree
D      5     no keyword, LLM request/access (uncalibrated)    low-calibrated-score / agree
E      5     no keyword, LLM network/outage P1                unconfirmed-high-severity /
                                                              3 agree (P1), 2 disagree (P2)
F      4     keyword subject, LLM returns garbage             llm-failure / disagree
G      3     unknown CMDB CI                                  unresolved-service / disagree
H      3     like A1, no human label                          routed / not counted
=====  ====  ===============================================  ==========================
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
START = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)

# (subject keyword phrase, preclass label it triggers)
KEYWORDS = [
    ("db connection refused", "database/failure"),
    ("deploy failed on rollout", "deploy/failure"),
    ("dns timeout from edge", "network/latency"),
    ("nightly dag failed", "batch/failure"),
    ("token rejected for service account", "auth/failure"),
]


def wilson(successes: int, n: int, z: float = 1.959963984540054) -> float:
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def nearest_rank(values: list[int], q: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def main() -> None:
    rows: list[dict[str, object]] = []
    script: dict[str, dict[str, object]] = {}
    expected: list[dict[str, object]] = []  # per incident: outcome facts for the golden

    def add(  # noqa: PLR0913 — one knob per fixture column
        group: str,
        subject: str,
        llm: tuple[str, str] | None,
        human: tuple[str, str] | None,
        *,
        ci: str = "CIPAYMENTS",
        reason: str = "routed",
        agree: bool | None = None,
        preclass: bool = True,
    ) -> None:
        i = len(rows) + 1
        number = f"INC{i:07d}"
        opened = START + timedelta(hours=i)
        latency = 2 + (i * 7) % 11  # 2..12 s of LLM latency, deterministic
        rows.append(
            {
                "record": {
                    "sys_id": f"sys{i:04d}",
                    "number": number,
                    "short_description": subject,
                    "description": f"Reported issue, reference {number}.",
                    "priority": "3",
                    "cmdb_ci": ci,
                    "opened_at": opened.strftime("%Y-%m-%d %H:%M:%S"),
                    "sys_updated_on": opened.strftime("%Y-%m-%d %H:%M:%S"),
                    "sys_updated_by": "partner.agent",
                    "sys_mod_count": "4",
                },
                "human": None
                if human is None
                else {
                    "classification": human[0],
                    "severity": human[1],
                    "first_action_at": (opened + timedelta(minutes=9)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                },
            }
        )
        if llm is None:
            script[number] = {"raw": "this is not json", "latency_s": latency}
        else:
            script[number] = {
                "raw": json.dumps(
                    {
                        "classification": llm[0],
                        "severity": llm[1],
                        "summary": f"{group} incident",
                        "confidence": 0.9,
                    }
                ),
                "latency_s": latency,
            }
        reached_llm = reason != "unresolved-service"
        expected.append(
            {
                "group": group,
                "reason": reason,
                "routed": reason == "routed",
                "latency": latency if reason == "routed" else None,
                "agree": agree,
                "human_severity": None if human is None else human[1],
                "reached_preclass": reason != "unresolved-service",
                "preclass_label": preclass and reached_llm,
            }
        )

    for k in range(15):  # A1
        phrase, label = KEYWORDS[k % 5]
        add("A1", phrase, (label, "P3"), (label, "P3"), agree=True)
    for _ in range(5):  # A2
        add(
            "A2",
            "db connection refused",
            ("database/failure", "P3"),
            ("database/latency", "P3"),
            agree=True,
        )
    for k in range(5):  # B
        phrase, label = KEYWORDS[k % 5]
        add("B", phrase, (label, "P3"), (label, "P2"), agree=False)
    for k in range(5):  # C
        human = ("network/outage", "P3") if k < 2 else ("deploy/failure", "P3")
        add(
            "C",
            "deploy failed on rollout",
            ("network/outage", "P3"),
            human,
            reason="classifier-conflict",
            agree=k < 2,
        )
    for _ in range(5):  # D
        add(
            "D",
            "user cannot open the shared drive",
            ("request/access", "P3"),
            ("request/access", "P3"),
            reason="low-calibrated-score",
            agree=True,
            preclass=False,
        )
    for k in range(5):  # E
        human = ("network/outage", "P1") if k < 3 else ("network/outage", "P2")
        add(
            "E",
            "customers report checkout errors",
            ("network/outage", "P1"),
            human,
            reason="unconfirmed-high-severity",
            agree=k < 3,
            preclass=False,
        )
    for _ in range(4):  # F
        add(
            "F",
            "db connection refused",
            None,
            ("database/failure", "P3"),
            reason="llm-failure",
            agree=False,
        )
    for _ in range(3):  # G
        add(
            "G",
            "db connection refused",
            ("database/failure", "P3"),
            ("database/failure", "P3"),
            ci="CIUNKNOWN",
            reason="unresolved-service",
            agree=False,
            preclass=False,
        )
    for k in range(3):  # H
        phrase, label = KEYWORDS[k % 5]
        add("H", phrase, (label, "P3"), None, agree=None)
    assert len(rows) == 50

    # --- the golden report, from the intended outcomes ----------------------
    labelled = [e for e in expected if e["agree"] is not None]
    agreed = sum(1 for e in labelled if e["agree"])

    def stat(items: list[dict[str, object]]) -> dict[str, object]:
        n, ok = len(items), sum(1 for e in items if e["agree"])
        return {
            "n": n,
            "agreed": ok,
            "rate": round(ok / n, 6),
            "wilson_lb": round(wilson(ok, n), 6),
        }

    high = [e for e in labelled if e["human_severity"] in ("P1", "P2")]
    low = [e for e in labelled if e["human_severity"] not in ("P1", "P2")]
    latencies = [int(e["latency"]) for e in expected if e["latency"] is not None]  # type: ignore[call-overload]
    safe: dict[str, int] = {}
    for e in expected:
        if e["reason"] != "routed":
            safe[str(e["reason"])] = safe.get(str(e["reason"]), 0) + 1
    reached = [e for e in expected if e["reached_preclass"]]
    with_label = [e for e in reached if e["preclass_label"]]
    conflicts = sum(1 for e in with_label if e["reason"] == "classifier-conflict")
    golden = {
        "mode": "shadow",
        "window_days": None,
        "n_cases": len(expected),
        "agreement": {
            "n": len(labelled),
            "agreed": agreed,
            "rate": round(agreed / len(labelled), 6),
            "wilson_lb": round(wilson(agreed, len(labelled)), 6),
        },
        "agreement_by_band": {"P1-P2": stat(high), "P3-P4": stat(low)},
        "first_proposal_latency_s": {
            "n": len(latencies),
            "p50": nearest_rank(latencies, 0.50),
            "p95": nearest_rank(latencies, 0.95),
        },
        "safe_default_by_reason": dict(sorted(safe.items())),
        "safe_default_rate": round(sum(safe.values()) / len(expected), 6),
        "abstain_rate": round((len(reached) - len(with_label)) / len(reached), 6),
        "conflict_rate": round(conflicts / len(with_label), 6),
    }

    (HERE / "incidents.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )
    (HERE / "llm_script.json").write_text(
        json.dumps(script, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    (HERE / "report_golden.json").write_text(
        json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
