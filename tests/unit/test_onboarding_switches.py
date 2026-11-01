"""ADR-0017 (WP-A6): switches for what onboarding changes at runtime, and keeping it current."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.files.plugin_loader import (
    load_registry,
    pin_one,
    set_disabled,
    write_index,
)
from supportagent.app.readiness_store import ReadinessStore
from supportagent.bootstrap import (
    PLUGINS_DIR,
    _fitted_calibration,
    _readiness_policy,
    _UnrecordedProvider,
    onboard_cycle,
)
from supportagent.domain.errors import ConfigError
from supportagent.evals.readiness import fit_calibration
from tests.unit.api_support import TENANT
from tests.unit.test_autopilot import _MASTER_KEY, _run
from tests.unit.test_readiness_routing import _replay
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.unit


def _config(**onboarding: str) -> Any:
    base = _shadow_config()
    return base.model_copy(
        update={
            "tenant": base.tenant.model_copy(update={"id": TENANT}),
            "onboarding": base.onboarding.model_copy(update=onboarding),
        }
    )


def test_report_mode_measures_readiness_without_letting_it_route(db: Any, clock: Any) -> None:
    store = ReadinessStore(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT)
    model = _config().llm.slots["classify"].model_id
    fitted = fit_calibration([_replay("app/memory", None, ok=True)], model_id=model, system="s")
    store.save(
        {
            "classes": [{"label": "app/memory", "hypothesis": "h", "status": "ready"}],
            "calibration": fitted.model_dump(mode="json"),
        }
    )
    uow = SqlUnitOfWork(db)
    assert _readiness_policy(_config(), uow, clock) is not None
    assert _readiness_policy(_config(routing="report"), uow, clock) is None
    assert _fitted_calibration(_config(), uow, clock) is not None
    assert _fitted_calibration(_config(calibration="artifact"), uow, clock) is None


def test_a_disabled_plugin_stays_pinned_but_isnt_loaded(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    shutil.copytree(PLUGINS_DIR, root)
    set_disabled(root, "kafka", disabled=True)
    assert "kafka" not in {p.manifest.id for p in load_registry(root)}
    write_index(root)  # a re-pin keeps it disabled
    pin_one(root, "oracle")
    assert "kafka" not in {p.manifest.id for p in load_registry(root)}
    set_disabled(root, "kafka", disabled=False)
    assert "kafka" in {p.manifest.id for p in load_registry(root)}
    with pytest.raises(ConfigError, match="not installed"):
        set_disabled(root, "made-up", disabled=True)


def test_a_watch_cycle_reruns_onboarding_and_checks_drift(
    tmp_path: Path, db: Any, clock: Any
) -> None:
    _run(tmp_path, db, clock)  # the first run ingests; a cycle then skips what's done
    status, findings = onboard_cycle(
        _config(),
        tmp_path / "system.yaml",
        tmp_path / "ws",
        engine=db,
        master_key=_MASTER_KEY,
        provider=_UnrecordedProvider(),
    )
    assert status.system == "shipment-tracking" and findings == []  # first drift: the baseline
    assert (tmp_path / "ws" / "onboarding_snapshot.json").is_file()
