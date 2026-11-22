"""WP-223: detectors measured by the instance's own onboarding run (ADR-0023 §2).

Canonical test IDs from 12-wp WP-223: ``test_step_without_input_is_done_not_blocked``
(and the detectors half of ``test_catalog_and_detectors_are_steps``, which this
file names ``test_detectors_are_a_step``; the catalog step lands with its review).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.bootstrap import (
    _UnrecordedProvider,
    detector_review_for,
    onboarding_control_for,
    run_autopilot,
)
from supportagent.domain.errors import DomainValidationError
from supportagent.domain.signal_detectors import STEP_S, Signals
from tests.unit.api_support import ADA, BOB, login, make_client
from tests.unit.test_in_instance_onboarding import _config, _manifest, _source

pytestmark = pytest.mark.unit

_MASTER_KEY = b"test" * 8
_STEPS = 7 * 24 * 3600 // STEP_S


class _History:
    """The deployment's Prometheus and Loki: the api's containers, logs every step but
    a 55-minute quiet each hour; the host's boot time."""

    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    def measure(self, components: Iterable[str], *, now: datetime, days: int) -> Signals:
        names = sorted(components)
        self.asked.append(names)
        hourly = [0.0 if (i % 12) < 11 else 40.0 for i in range(_STEPS)]
        return Signals(
            containers=frozenset({"api", "db"} & set(names)),
            host=True,
            log_volume={"api": hourly, "db": [None] * _STEPS},
            start_ts=int(now.timestamp()) - days * 86400,
        )


def _run(tmp_path: Path, db: Any, clock: Any, history: _History | None) -> dict[str, Any]:
    config = _config(tmp_path)
    if not (tmp_path / "system").exists():
        onboarding_control_for(config, db, clock).save_manifest(
            _manifest(_source(tmp_path)), actor="ada"
        )
    status = run_autopilot(
        config,
        engine=db,
        master_key=_MASTER_KEY,
        clock=clock,
        llm_provider=_UnrecordedProvider(),
        signal_history=history,
    )
    assert status is not None
    return {"steps": {s.step: s for s in status.steps}, "needs": status.needs}


def test_step_without_input_is_done_not_blocked(tmp_path: Path, db: Any, clock: Any) -> None:
    run = _run(tmp_path, db, clock, history=None)
    step = run["steps"]["detectors"]
    assert step.state == "done" and step.detail.startswith("nothing to do"), step.detail
    assert run["steps"]["incidents"].state == "done", "no ticket history: nothing to do"


def test_detectors_are_a_step(tmp_path: Path, db: Any, clock: Any) -> None:
    history = _History()
    run = _run(tmp_path, db, clock, history)
    step = run["steps"]["detectors"]
    assert step.state == "done" and "detector(s) measured over 7 days" in step.detail
    assert history.asked == [["api", "db"]], "the components the graph found"
    review = detector_review_for(_config(tmp_path), db, clock)
    ids = {d["id"] for d in review.state()["detectors"]}
    assert {"container-down/api", "crash-loop/api", "log-silence/api", "host-restart/host"} <= ids
    silence = next(d for d in review.state()["detectors"] if d["id"] == "log-silence/api")
    assert silence["measured"]["threshold_min"] == 110, "twice the hourly 55-minute quiet"
    assert any(n["subject"] == "db" for n in review.state()["not_proposed"]), "no history"
    assert any(n.how == "/ui/onboarding/detectors" for n in run["needs"]), "to review"

    clock.advance(60)
    again = _run(tmp_path, db, clock, history)
    assert again["steps"]["detectors"].at == step.at, "same day, same components: skipped"
    clock.advance(86400)
    _run(tmp_path, db, clock, history)
    assert len(history.asked) == 2, "the history grew: measured again the next day"


def test_enabled_detectors_survive_a_new_measurement(
    tmp_path: Path, db: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    _run(tmp_path, db, clock, _History())
    oidc = FakeOIDC()
    http = make_client(db, clock, ids, master_key, oidc)
    login(http, oidc, ADA)
    page = http.get("/ui/onboarding/detectors").text
    assert "fires after 110 min" in page and 'name="on:log-silence/api"' in page
    token = http.cookies["sa_csrf"]
    saved = http.post(
        "/ui/onboarding/detectors",
        data={"csrf_token": token, "on:log-silence/api": "1", "on:container-down/api": "1"},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"].endswith("saved=2")
    review = detector_review_for(_config(tmp_path), db, clock)
    assert {d.id for d in review.enabled("internal")} == {"log-silence/api", "container-down/api"}
    assert review.unseen() == [], "looked at"
    with pytest.raises(DomainValidationError, match="no such detector"):
        review.decide(["log-silence/nope"], actor="ada")

    # The next day's measurement no longer proposes the api's silence detector: still enabled.
    clock.advance(86400)
    quiet = _History()
    quiet.measure = lambda c, *, now, days: Signals(containers=frozenset({"api"}))  # type: ignore[method-assign]
    _run(tmp_path, db, clock, quiet)
    kept = {d.id for d in review.enabled()}
    assert kept == {"log-silence/api", "container-down/api"}
    stale = next(d for d in review.state()["detectors"] if d["id"] == "log-silence/api")
    assert stale["stale"] is True

    login(http, oidc, ADA)  # a day later: a new session
    component = http.get("/ui/system/component?id=service:api").text
    assert "its container is not running" in component, "a component lists its detectors"
    login(http, oidc, BOB)
    refused = http.post(
        "/ui/onboarding/detectors",
        data={"csrf_token": http.cookies["sa_csrf"]},
        follow_redirects=False,
    )
    assert refused.status_code == 403, "only an administrator enables detectors"
    assert {d.id for d in review.enabled()} == kept
