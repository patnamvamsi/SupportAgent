"""ADR-0017 (WP-A2): plugins come from a catalogue; onboarding installs what it detects.

System C's three plugins (nodejs, mongodb, rabbitmq) are taken out of the
installed plugins and the packs, and put in a catalogue, each one directory
with its pack inside. Onboarding the system must find them, install them,
prove each on its own lab here, and use them, with nothing else changed.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from supportagent.adapters.files.eval_loader import load_yaml_document
from supportagent.adapters.files.plugin_loader import load_registry, write_index
from supportagent.bootstrap import (
    _REPO_ROOT,
    ONBOARDING_DIR,
    PLUGINS_DIR,
    _catalog_of,
    install_from_catalog,
    onboard_run,
)
from supportagent.domain.errors import ConfigError, LLMUnavailable
from supportagent.ports.llm import ProviderRequest, ProviderResponse
from tests.unit.api_support import TENANT
from tests.unit.test_autopilot import _transports
from tests.unit.test_ingest import _ENV, _manifest
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.unit

_MOVED = ("nodejs", "mongodb", "rabbitmq")
_SYSTEM = ONBOARDING_DIR / "system-c"


class _LabOnly:
    """Answers the plugins' lab scenarios as their truth says; nothing else (no model)."""

    def __init__(self, catalog: Path) -> None:
        self._labels: dict[str, str] = {}
        for scenario in catalog.glob("*/pack/evals/*.yaml"):
            raw = load_yaml_document(scenario)
            self._labels[raw["incident"]["subject"]] = raw["truth"]["classification"]

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        if "triage classifier" in request.system:
            label = next((v for k, v in self._labels.items() if k in request.user), None)
            if label is None:
                raise LLMUnavailable("not a lab scenario")  # noqa: TRY003
            body: dict[str, Any] = {
                "classification": label,
                "severity": "P2",
                "summary": "s",
                "confidence": 0.9,
            }
        elif "root-cause investigation" in request.system:
            body = {"hypotheses": []}
        elif "check whether evidence supports a claim" in request.system:
            body = {"verdict": "supports"}
        else:
            raise LLMUnavailable("onboarding has no model here")  # noqa: TRY003
        return ProviderResponse(json.dumps(body), request.model_id, 1, 1, 1)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    root, catalog, packs = tmp_path / "plugins", tmp_path / "catalog", tmp_path / "packs"
    shutil.copytree(PLUGINS_DIR, root, ignore=shutil.ignore_patterns(*_MOVED))
    shutil.copytree(
        _REPO_ROOT / "packs", packs, ignore=shutil.ignore_patterns(*_MOVED, "recordings")
    )
    for plugin in _MOVED:
        shutil.copytree(PLUGINS_DIR / plugin, catalog / plugin)
        shutil.copytree(
            _REPO_ROOT / "packs" / plugin,
            catalog / plugin / "pack",
            ignore=shutil.ignore_patterns("recordings", "baseline.json"),
        )
    write_index(root)
    write_index(catalog)
    monkeypatch.setenv("SUPPORTAGENT_PLUGINS_DIR", str(root))
    _catalog_of.cache_clear()
    yield {"root": root, "catalog": catalog, "packs": packs, "tmp": tmp_path}
    _catalog_of.cache_clear()


def _config(world: dict[str, Path], auto: str) -> Any:
    base = _shadow_config()
    return base.model_copy(
        update={
            "tenant": base.tenant.model_copy(update={"id": TENANT}),
            "paths": base.paths.model_copy(update={"packs": str(world["packs"])}),
            "plugins": base.plugins.model_copy(
                update={"catalogs": [str(world["catalog"])], "auto_install": auto}
            ),
        }
    )


def _run(world: dict[str, Path], auto: str, db: Any, clock: Any) -> Any:
    return onboard_run(
        _config(world, auto),
        _manifest(_SYSTEM, world["tmp"]),
        world["tmp"] / "ws",
        provider=_LabOnly(world["catalog"]),
        engine=db,
        clock=clock,
        transports=_transports(),
        env=_ENV,
    )


def test_onboarding_installs_what_it_detects_from_a_catalogue(
    world: dict[str, Path], db: Any, clock: Any
) -> None:
    assert not any((world["root"] / p).exists() for p in _MOVED)
    status = _run(world, "first-party", db, clock)
    sense = next(s for s in status.steps if s.step == "sense")
    assert sense.state == "done", status.lines()
    for plugin in _MOVED:
        assert f"{plugin} (evaluated" in sense.detail  # each proved on its own lab here
    installed = {p.manifest.id: p for p in load_registry(world["root"])}
    assert set(_MOVED) <= set(installed)
    assert all(installed[p].manifest.status == "evaluated" for p in _MOVED)
    access = {n.what for n in status.needs if n.kind == "access"}
    assert {"mongodb/mongodb_ro on shipmentsdb", "rabbitmq/rabbitmq_ro on logistics-mq"} <= access


def test_without_auto_install_each_detected_plugin_waits_for_approval(
    world: dict[str, Path], db: Any, clock: Any
) -> None:
    status = _run(world, "none", db, clock)
    sense = next(s for s in status.steps if s.step == "sense")
    assert (sense.state, sense.on) == ("blocked", "user")
    asked = {n.how for n in status.needs if n.kind == "plugin"}
    assert {f"supportagent plugin install {p}" for p in _MOVED} <= asked
    assert not any((world["root"] / p).exists() for p in _MOVED)
    result = install_from_catalog(
        _config(world, "none"), "mongodb", provider=_LabOnly(world["catalog"])
    )
    assert result.status == "evaluated"
    again = _run(world, "none", db, clock)
    asked = {n.how for n in again.needs if n.kind == "plugin"}
    assert "supportagent plugin install mongodb" not in asked
    assert "supportagent plugin install nodejs" in asked


def test_an_installed_plugin_is_pinned_whole(world: dict[str, Path]) -> None:
    install_from_catalog(_config(world, "none"), "rabbitmq", provider=_LabOnly(world["catalog"]))
    runbook = world["root"] / "rabbitmq" / "pack" / "runbooks" / "rabbit-backlog.yaml"
    runbook.write_text(runbook.read_text() + "# tampered\n")
    with pytest.raises(ConfigError, match="its files changed"):
        load_registry(world["root"])


def test_only_a_configured_catalogue_can_be_installed_from(world: dict[str, Path]) -> None:
    with pytest.raises(ConfigError, match="no configured catalogue offers"):
        install_from_catalog(_config(world, "none"), "made-up")
